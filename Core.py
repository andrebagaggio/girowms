"""Lógica de períodos, API do Infor WMS e análise de endereçamento."""
import math
from datetime import date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SUFIXOS_CHAO = ("000", "010")
SUFIXOS_MEDIO = ("020",)
PREFIXOS_VALIDOS = ("RAIA", "01")

# Cada filial tem o seu storerkey e o seu owner da API de estoque.
# ATENÇÃO: confirme o owner da 011005 com "Testar API de estoque" na sidebar.
FILIAIS = {
    "011324": {"storerkey": "BAG_ONLINE", "owner": "bag_online"},
    "011005": {"storerkey": "IMP-F", "owner": "imp-f"},
}


# Filial que abastece cada filial (usada na aba Ruptura para ver se dá para ser abastecido).
FILIAL_ABASTECEDORA = {"011324": "011005"}


def dados_filial(filial) -> dict:
    """Storerkey e owner da filial; erro claro se a filial não estiver cadastrada."""
    codigo = str(filial or "").strip()
    dados = FILIAIS.get(codigo)
    if not dados:
        validas = ", ".join(sorted(FILIAIS))
        raise ValueError(f"Filial '{codigo}' sem cadastro de storerkey/owner (válidas: {validas}).")
    return dados


def somar_meses(d: date, n: int) -> date:
    idx = d.year * 12 + (d.month - 1) + n
    return date(idx // 12, idx % 12 + 1, 1)


def periodo_meses_fechados(hoje: date, meses: int):
    fim = hoje.replace(day=1)
    return somar_meses(fim, -meses), fim


def periodo_ultimo_mes(hoje: date):
    return periodo_meses_fechados(hoje, 1)


def periodo_ultimos_dias(hoje: date, dias: int = 30):
    return hoje - timedelta(days=dias - 1), hoje + timedelta(days=1)


def fmt_sql(d: date) -> str:
    return d.strftime("%Y%m%d")


def criar_sessao() -> requests.Session:
    sessao = requests.Session()
    retry = Retry(total=3, backoff_factor=0.6,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=None)
    sessao.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=10))
    return sessao


def obter_token(session: requests.Session, ionapi: dict) -> str:
    resposta = session.post(
        ionapi["pu"] + ionapi["ot"],
        data={"grant_type": "password", "client_id": ionapi["ci"],
              "client_secret": ionapi["cs"], "username": ionapi["saak"],
              "password": ionapi["sask"]},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30,
    )
    resposta.raise_for_status()
    return resposta.json()["access_token"]


def extrair_lista(js):
    if isinstance(js, list):
        return js
    if isinstance(js, dict):
        for chave in ("countdetails", "results", "items", "data", "rows",
                      "lotxlocxid", "inventory", "content"):
            valor = js.get(chave)
            if isinstance(valor, list):
                return valor
        listas = [v for v in js.values() if isinstance(v, list) and v
                  and isinstance(v[0], dict)]
        if listas:
            return max(listas, key=len)
    return []


def buscar_estoque_raw(sku: str, cfg: dict):
    url = f"{cfg['base']}/{cfg['path']}"
    headers = {"Authorization": f"Bearer {cfg['token']}",
               "Accept": "application/json", "Content-Type": "application/json"}
    filtro = {cfg["campo_sku"]: sku}
    if cfg.get("owner"):
        filtro[cfg.get("campo_owner", "owner")] = cfg["owner"]
    metodo = cfg["method"].upper()
    if cfg.get("envio", "query") == "query" or metodo == "GET":
        r = cfg["session"].request(metodo, url, headers=headers, params=filtro, timeout=60)
    else:
        r = cfg["session"].post(url, headers=headers, json=filtro, timeout=60)
    if r.status_code == 401:
        raise PermissionError("Token expirado ou inválido (401).")
    r.raise_for_status()
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


def _num(valor) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


def zona_endereco(loc: str) -> str:
    endereco = (loc or "").upper().strip()
    if endereco.startswith("RAIA"):
        return "chão"
    if endereco.endswith(SUFIXOS_CHAO):
        return "chão"
    if endereco.endswith(SUFIXOS_MEDIO):
        return "médio"
    return "alto"


def normalizar_estoque(lista, sku: str, ignorar=()):
    ignorados = {str(x).strip().upper() for x in ignorar if str(x).strip()}
    sku_ref = str(sku).strip().upper()
    linhas = []
    for bruto in lista or []:
        if not isinstance(bruto, dict):
            continue
        r = {str(k).lower(): v for k, v in bruto.items()}
        item = str(r.get("sku") or r.get("item") or sku).strip().upper()
        if item != sku_ref:
            continue
        loc = str(r.get("loc") or r.get("location") or "").strip()
        if not loc or loc.upper() in ignorados or not loc.upper().startswith(PREFIXOS_VALIDOS):
            continue
        qty = _num(next((r[k] for k in ("qty", "quantity", "onhand")
                         if r.get(k) is not None), 0))
        if qty <= 0:
            continue
        reservado = sum(_num(r.get(k)) for k in
                        ("qtyallocated", "qtypicked", "qtypickinprocess"))
        status = str(r.get("status") or "OK").strip().upper()
        linhas.append({
            "loc": loc, "id": str(r.get("id") or "").strip(),
            "lot": str(r.get("lot") or "").strip(),
            "zona": zona_endereco(loc), "qty": qty, "status": status,
            "disp": max(qty - reservado, 0.0) if status == "OK" else 0.0,
        })
    return linhas


def _fmt_locs(linhas, n=4):
    agrupado = {}
    for linha in linhas:
        agrupado[linha["loc"]] = agrupado.get(linha["loc"], 0) + linha["disp"]
    maiores = sorted(agrupado.items(), key=lambda par: -par[1])[:n]
    return ", ".join(f"{loc} ({q:,.0f})".replace(",", ".")
                     for loc, q in maiores if q > 0)


def analisar_estoque(sku, linhas, media_dia, dias_cobertura, modo):
    """Calcula retirada do chão ou reposição, sem criar ordens no WMS."""
    chao_l = [l for l in linhas if l["zona"] == "chão"]
    reserva_l = [l for l in linhas if l["zona"] != "chão"]
    chao = sum(l["disp"] for l in chao_l)
    reserva = sum(l["disp"] for l in reserva_l)
    meta = math.ceil(max(float(media_dia), 0) * dias_cobertura)

    if modo == "descer":
        falta = max(meta - chao, 0)
        mover = min(falta, reserva)
        if chao == 0 and reserva == 0:
            status = "Sem estoque disponível"
        elif falta == 0:
            status = "OK: chão cobre a meta"
        elif mover == 0:
            status = f"Chão abaixo da meta, sem saldo acima (faltam {falta:,.0f})"
        elif mover < falta:
            status = f"Descer parcial: {mover:,.0f} (faltam {falta-mover:,.0f})"
        else:
            status = f"Descer {mover:,.0f}"
        origem, destino = _fmt_locs(reserva_l), _fmt_locs(chao_l)
    elif modo == "subir":
        mover = max(chao - meta, 0)
        status = f"Subir {mover:,.0f}" if mover > 0 else "OK: nada a subir"
        origem, destino = _fmt_locs(chao_l), _fmt_locs(reserva_l)
    else:
        raise ValueError(f"Modo não reconhecido: {modo}")

    return {"sku": sku, "status": status.replace(",", "."),
            "chao": chao, "reserva": reserva, "meta": meta, "mover": mover,
            "origem": origem, "destino": destino, "detalhe": linhas}


# ----------------------------------------------------------------------------
# Ruptura de estoque
# ----------------------------------------------------------------------------
FAIXAS = ("Crítico", "Alerta", "Atenção", "OK")
JANELA_RECENTE = 7      # dias completos da janela recente
JANELA_CURTA = 3        # últimos dias, para ver se o pico continua
JANELA_BASE = 28        # dias da linha de base (antes da janela recente)
FATOR_CONTINUA = 1.2    # últimos 3 dias precisam estar >= 1,2x a base para o pico "continuar"


def _faixa(cobertura: float, limites) -> str:
    critico, alerta, atencao = limites
    if cobertura < critico:
        return "Crítico"
    if cobertura < alerta:
        return "Alerta"
    if cobertura < atencao:
        return "Atenção"
    return "OK"


def analisar_ruptura(sku, linhas, demanda, pendente=0.0, limites=(3, 7, 15),
                     fator_pico=1.5, hoje=None, dias_alvo=15, min_vol=0):
    """Classifica o risco de ruptura de um SKU. Não cria nada no WMS.

    demanda: dict com as saídas. Chaves brutas: qtd_7d, qtd_base. Chaves ajustadas
             (cada linha de pedido limitada ao teto de "venda inesperada"): qtd_7d_aj,
             qtd_3d_aj, qtd_base_aj. Também maior_linha_7d (maior linha de pedido
             nos últimos 7 dias). O pico e a cobertura usam os valores AJUSTADOS, para uma
             venda pontual muito grande não ser confundida com aumento de procura.
    min_vol: saída mínima (peças em 7 dias) para considerar pico.
    pendente: peças de NF de entrada ainda não conferidas.
    dias_alvo: cobertura que se quer ter; a necessidade é o que falta para chegar nela.
    """
    hoje = hoje or date.today()
    def _val(chave, padrao):
        return _num(demanda[chave]) if demanda.get(chave) is not None else padrao

    q7_bruto = _num(demanda.get("qtd_7d"))
    qb_bruto = _val("qtd_base", 0.0)
    q7 = _val("qtd_7d_aj", q7_bruto)        # ajustado: sem o excesso de vendas inesperadas
    q3 = _val("qtd_3d_aj", _val("qtd_3d", 0.0))
    qb = _val("qtd_base_aj", qb_bruto)
    maior_linha = _val("maior_linha_7d", 0.0)
    excesso_7d = max(q7_bruto - q7, 0.0)

    taxa_7d = q7 / JANELA_RECENTE
    taxa_3d = q3 / JANELA_CURTA
    taxa_base = qb / JANELA_BASE
    razao = (taxa_7d / taxa_base) if taxa_base > 0 else None

    # Pico calculado com os números brutos, só para saber se a venda pontual o "criava".
    taxa_7d_bruta = q7_bruto / JANELA_RECENTE
    taxa_base_bruta = qb_bruto / JANELA_BASE
    pico_bruto = q7_bruto > 0 and q7_bruto >= min_vol and (
        taxa_base_bruta == 0 or taxa_7d_bruta >= fator_pico * taxa_base_bruta)

    # Pico (ajustado): demanda recente bem acima da base (ou SKU sem histórico com saída agora).
    pico = q7 > 0 and q7 >= min_vol and (taxa_base == 0 or taxa_7d >= fator_pico * taxa_base)
    continua = q3 > 0 and (taxa_base == 0 or taxa_3d >= FATOR_CONTINUA * taxa_base)
    if pico and taxa_base == 0:
        tipo = "Sem histórico"
    elif pico and continua:
        tipo = "Pico em curso"
    elif pico:
        tipo = "Pico perdendo força"
    elif pico_bruto:
        tipo = "Venda pontual"   # o "pico" vinha de uma venda inesperada, não da procura
    else:
        tipo = "Normal"

    # Taxa de referência conservadora: a maior entre a recente e a base.
    taxa_ref = max(taxa_7d, taxa_base)

    chao_l = [l for l in linhas if l["zona"] == "chão"]
    reserva_l = [l for l in linhas if l["zona"] != "chão"]
    chao = sum(l["disp"] for l in chao_l)
    reserva = sum(l["disp"] for l in reserva_l)
    total = chao + reserva
    # Saldo físico que existe mas não está disponível (HOLD/alocado/separado).
    indisponivel = sum(max(l["qty"] - l["disp"], 0.0) for l in linhas)
    pendente = max(_num(pendente), 0.0)

    if taxa_ref <= 0:
        cobertura = math.inf
        cobertura_ent = math.inf
        chao_cob = math.inf
    else:
        cobertura = total / taxa_ref
        cobertura_ent = (total + pendente) / taxa_ref
        chao_cob = chao / taxa_ref

    # O que falta para atingir a cobertura alvo, já descontando a NF pendente.
    necessidade = max(math.ceil(taxa_ref * dias_alvo - (total + pendente)), 0) if taxa_ref > 0 else 0

    faixa = _faixa(cobertura, limites)
    faixa_ent = _faixa(cobertura_ent, limites)
    melhorou = FAIXAS.index(faixa_ent) > FAIXAS.index(faixa)

    if faixa != "OK":
        if faixa_ent == "OK":
            situacao = "Coberta por entrada"
        elif melhorou:
            situacao = "Parcialmente coberta por entrada"
        else:
            situacao = "Ruptura"
    elif chao_cob < limites[1] and reserva > 0:
        situacao = "Ruptura de picking"
    else:
        situacao = "OK"

    # Ação sugerida
    if total == 0 and pendente == 0:
        acao = "Sem saldo nem entrada: acionar compra/produção"
    elif total == 0:
        acao = "Zerado: priorizar conferência da NF pendente"
    elif situacao == "Ruptura":
        acao = ("Entrada pendente não cobre: priorizar recebimento e repor"
                if pendente > 0 else "Sem entrada prevista: repor/comprar")
    elif situacao == "Parcialmente coberta por entrada":
        acao = "Priorizar conferência da NF; ainda falta reposição"
    elif situacao == "Coberta por entrada":
        acao = "Priorizar conferência da NF para a entrada chegar a tempo"
    elif situacao == "Ruptura de picking":
        acao = "Descer da reserva para o chão"
    else:
        acao = "—"
    sufixo_acao = ""
    if indisponivel > 0 and situacao not in ("OK", "Ruptura de picking"):
        sufixo_acao = f" (há {indisponivel:,.0f} indisponíveis por HOLD/alocação)".replace(",", ".")
    acao += sufixo_acao

    data_ruptura = None
    if math.isfinite(cobertura):
        data_ruptura = hoje + timedelta(days=int(math.floor(cobertura)))

    return {
        "sku": sku, "erro": False,
        "faixa": faixa, "faixa_entrada": faixa_ent, "situacao": situacao,
        "tipo": tipo, "pico": pico, "continua": continua,
        "excesso_7d": excesso_7d, "maior_linha_7d": maior_linha,
        "venda_inesperada": excesso_7d > 0,
        "taxa_7d": taxa_7d, "taxa_3d": taxa_3d, "taxa_base": taxa_base,
        "razao": razao, "taxa_ref": taxa_ref,
        "chao": chao, "reserva": reserva, "total": total,
        "indisponivel": indisponivel, "pendente": pendente,
        "cobertura": cobertura, "cobertura_entrada": cobertura_ent,
        "necessidade": necessidade, "dias_alvo": dias_alvo,
        "data_ruptura": data_ruptura, "acao": acao, "sufixo_acao": sufixo_acao,
        "detalhe": linhas,
        "status": f"{faixa} · {situacao}",
    }


def avaliar_abastecimento(res: dict, forn_disp: float, fornecedora: str):
    """Compara a necessidade do SKU com o saldo disponível na filial abastecedora."""
    necessidade = float(res.get("necessidade", 0))
    forn_disp = max(float(forn_disp or 0), 0.0)
    atende = min(necessidade, forn_disp)
    if necessidade <= 0:
        status = "Sem necessidade"
    elif forn_disp >= necessidade:
        status = "Atende total"
    elif forn_disp > 0:
        status = "Atende parcial"
    else:
        status = "Não atende"
    res.update({"fornecedora": fornecedora, "forn_disp": forn_disp,
                "forn_atende": atende, "forn_status": status})

    if res["situacao"] in ("Ruptura", "Parcialmente coberta por entrada"):
        sufixo = res.get("sufixo_acao", "")
        if status == "Atende total":
            res["acao"] = f"Pedir {necessidade:,.0f} peças à {fornecedora}".replace(",", ".") + sufixo
        elif status == "Atende parcial":
            res["acao"] = (f"Pedir {atende:,.0f} à {fornecedora}; faltam {necessidade - atende:,.0f} "
                           "(compra/produção)").replace(",", ".") + sufixo
        elif status == "Não atende":
            res["acao"] = f"{fornecedora} sem saldo: acionar compra/produção" + sufixo
    return res


def ordem_ruptura(res: dict):
    """Chave de ordenação: pior faixa primeiro, picos em curso antes, menor cobertura."""
    pico_urgente = 0 if res.get("tipo") in ("Pico em curso", "Sem histórico") else 1
    return (FAIXAS.index(res["faixa"]), pico_urgente, res["cobertura"])


# Limites operacionais por categoria do produto (DESC_GRUPO).
LIMITES_CATEGORIA = {
    "COTIDIANO": 50,
    "MALAS": 45,
    "MOCHILAS": 150,
    "OUTROS": 40,
    "VIAGEM": 60,
}


def planejar_movimentacoes(sku, linhas, quantidade, modo, filial, categoria=None, destino_manual=None, ocupacao_manual=0):
    """Planeja sugestões de movimentação por SKU/endereço, sem enviar ao WMS.

    Escolhe primeiro o destino com menor quantidade atual do SKU, esgotando
    sua capacidade antes de avançar ao próximo. Não presume endereços vazios,
    nem envia excedentes sem capacidade identificada.
    """
    import unicodedata

    storerkey = dados_filial(filial)["storerkey"]
    if modo not in ("subir", "descer"):
        raise ValueError("Modo inválido.")
    valor = float(quantidade)
    if not math.isfinite(valor) or valor <= 0 or not valor.is_integer():
        raise ValueError("A quantidade sugerida precisa ser um inteiro positivo.")

    categoria_limpa = unicodedata.normalize(
        "NFKD", str(categoria or "").strip().upper()
    )
    categoria_limpa = "".join(c for c in categoria_limpa if not unicodedata.combining(c))
    if categoria_limpa not in LIMITES_CATEGORIA:
        raise ValueError(f"Categoria '{categoria or 'não informada'}' sem limite cadastrado. Não é seguro gerar tarefas.")
    limite = LIMITES_CATEGORIA[categoria_limpa]

    origem_chao = modo == "subir"
    origens = [l for l in linhas if float(l.get("disp", 0)) >= 1
               and (l.get("zona") == "chão") == origem_chao]
    destinos = [l for l in linhas if l.get("loc")
                and (l.get("zona") == "chão") != origem_chao]
    if not origens:
        raise ValueError("Nenhuma origem com saldo disponível na zona correspondente.")

    # Soma saldo físico (qty), e não disponível (disp), pois peças alocadas
    # também ocupam a posição. Contabiliza lotes/LPNs presentes no mesmo endereço.
    ocupacao = {}
    for l in destinos:
        loc = str(l["loc"]).strip()
        ocupacao[loc] = ocupacao.get(loc, 0.0) + float(l.get("qty", 0))
    livres = {loc: max(0, int(math.floor(limite - qtd)))
              for loc, qtd in ocupacao.items()}
    if destino_manual:
        destino_manual = str(destino_manual).strip().upper()
        # Destinos manuais são permitidos apenas na descida, com capacidade declarada.
        if modo != "descer":
            raise ValueError("Destino manual está disponível somente na aba Descer.")
        if not destino_manual.startswith(PREFIXOS_VALIDOS) or zona_endereco(destino_manual) != "chão":
            raise ValueError("Destino manual precisa ser um endereço válido de chão/RAIA.")
        if destino_manual in {str(l["loc"]).strip().upper() for l in origens}:
            raise ValueError("O destino manual não pode ser um endereço de origem.")
        valor_ocupacao = float(ocupacao_manual)
        if not math.isfinite(valor_ocupacao) or valor_ocupacao < 0 or not valor_ocupacao.is_integer():
            raise ValueError("Informe a quantidade inteira atual do SKU no endereço manual.")
        # Se a API já retornou saldo nessa posição, o valor físico tem precedência.
        ocupacao_conhecida = ocupacao.get(destino_manual, 0)
        ocupacao[destino_manual] = max(float(valor_ocupacao), ocupacao_conhecida)
        livres[destino_manual] = max(0, int(math.floor(limite - ocupacao[destino_manual])))
    if not livres:
        raise ValueError("Nenhum destino encontrado. Na aba Descer, informe um destino manual.")
    # Posição com menos peças do SKU é atendida primeiro.
    ordem_destinos = sorted(livres, key=lambda loc: (ocupacao[loc], loc))
    # IDs ausentes são aceitos pela API como string vazia.
    origens.sort(key=lambda l: (-float(l["disp"]), str(l["loc"]), str(l.get("lot", "")), str(l.get("id", ""))))

    restante = int(valor)
    tarefas = []
    for linha in origens:
        if restante <= 0:
            break
        loc_origem = str(linha["loc"]).strip()
        qtd_origem = min(int(math.floor(float(linha["disp"]))), restante)
        for destino in ordem_destinos:
            if qtd_origem <= 0:
                break
            if destino == loc_origem or livres[destino] <= 0:
                continue
            qtd = min(qtd_origem, livres[destino], restante)
            if qtd <= 0:
                continue
            tarefas.append({
                "fromloc": loc_origem, "fromid": str(linha.get("id") or "").strip(),
                "lot": str(linha.get("lot") or ""), "qty": qtd, "sku": str(sku), "storerkey": storerkey,
                "tasktype": "MV", "toloc": destino, "priority": "5",
            })
            livres[destino] -= qtd
            qtd_origem -= qtd
            restante -= qtd
    if not tarefas:
        raise ValueError(
            f"Não foi encontrada capacidade livre nos destinos conhecidos para a categoria {categoria_limpa} "
            f"(limite de {limite} peças por posição)."
        )
    return tarefas
