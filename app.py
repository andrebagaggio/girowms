from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd
import pymssql
import streamlit as st

# Carrega SEMPRE o core.py desta mesma pasta, evitando importar outro módulo "core".
import importlib.util
from pathlib import Path

CORE_PATH = Path(__file__).resolve().with_name("core.py")
if not CORE_PATH.is_file():
    raise RuntimeError(f"Arquivo obrigatório não encontrado: {CORE_PATH}")
_especificacao = importlib.util.spec_from_file_location("giro_wms_core_local", CORE_PATH)
if _especificacao is None or _especificacao.loader is None:
    raise RuntimeError(f"Não foi possível carregar {CORE_PATH}")
core = importlib.util.module_from_spec(_especificacao)
_especificacao.loader.exec_module(core)
if not all(callable(getattr(core, nome, None))
           for nome in ("planejar_movimentacoes", "dados_filial", "analisar_ruptura")):
    raise RuntimeError(
        f"O arquivo carregado ({CORE_PATH}) é antigo e NÃO contém "
        "planejar_movimentacoes/dados_filial/analisar_ruptura. Substitua core.py pelo arquivo "
        "deste pacote e reinicie o Streamlit."
    )

st.set_page_config(page_title="Giro x Endereçamento - Infor WMS", layout="wide")

# Visual compacto: menos espaço vazio, cartões de indicadores e tabelas mais densas.
st.markdown(
    """
<style>
.block-container{padding-top:1.1rem;padding-bottom:1rem;max-width:1500px}
h1{font-size:1.55rem !important;padding:0 0 .1rem 0 !important}
h5{margin:.5rem 0 .1rem 0 !important}
div[data-testid="stVerticalBlock"]{gap:.65rem}
div[data-testid="stMetric"]{border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:.4rem .7rem}
div[data-testid="stMetricValue"]{font-size:1.35rem}
div[data-testid="stMetricLabel"] p{font-size:.78rem}
button[data-baseweb="tab"]{padding:.35rem .9rem}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:.5rem}
.kpi{border:1px solid rgba(128,128,128,.25);border-left:4px solid var(--c,#888);border-radius:10px;padding:.4rem .7rem}
.kpi b{font-size:1.35rem;display:block;line-height:1.2}
.kpi span{font-size:.74rem;opacity:.75}
.guia-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.45rem;margin:.15rem 0 .5rem 0}
.guia-card{border:1px solid rgba(128,128,128,.25);border-left:4px solid var(--c,#888);border-radius:10px;padding:.45rem .65rem;font-size:.8rem;line-height:1.35}
.guia-card b{display:block;margin-bottom:.1rem;font-size:.84rem}
</style>
""",
    unsafe_allow_html=True,
)

MESES_RANK = 3        # janela do ranking de recorrência no top 10
TOP_MENSAL = 10       # tamanho do top mensal usado para medir recorrência
MAX_WORKERS = 4       # teto de chamadas simultâneas à API

st.session_state.setdefault("dados", {})
st.session_state.setdefault("res", {"descer": {}, "subir": {}})
st.session_state.res.setdefault("ruptura", {})


# ----------------------------------------------------------------------------
# Conexões
# ----------------------------------------------------------------------------
@st.cache_resource
def get_session():
    return core.criar_sessao()


@st.cache_data(ttl=1500, show_spinner=False)
def get_token():
    return core.obter_token(get_session(), dict(st.secrets["ionapi"]))


def montar_cfg(filial=None):
    """Configuração da API de estoque. O owner vem da filial informada ou, se omitida, da sidebar."""
    w = st.secrets["wms"]
    filial_atual = str(filial or st.session_state.get("filial", "011324")).strip()
    owner = core.dados_filial(filial_atual)["owner"]
    return {
        "base": w["base_url"].rstrip("/"),
        "path": w.get("inventory_path", "inventorybalance/showinventorycount").strip("/"),
        "method": str(w.get("inventory_method", "POST")).upper(),
        "envio": w.get("inventory_envio", "query"),
        "campo_sku": w.get("inventory_campo_sku", "item"),
        "owner": owner,
        "token": get_token(),
        "session": get_session(),
    }


def consultar_sql(sql, params):
    c = st.secrets["sql"]
    conn = pymssql.connect(
        server=c["server"],
        user=c["user"],
        password=c["password"],
        database=c["database"],
        port=str(c.get("port", 1433)),
        login_timeout=15,
        timeout=120,
    )
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
        # Padroniza os nomes devolvidos pelo driver SQL Server.
        df.columns = [str(col).strip().upper() for col in df.columns]
        return df
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Cadastro de produtos via linked server
# ----------------------------------------------------------------------------
def sql_produtos_openquery():
    """Consulta cadastral; nome do linked server configurado em secrets.toml."""
    linked_server = str(st.secrets["sql"].get("linked_server", "")).strip()
    if not linked_server:
        raise ValueError(
            "Configure linked_server na seção [sql] do secrets.toml "
            "para consultar ZVIEW_SKU via OPENQUERY."
        )
    # Quote seguro do identificador T-SQL do linked server.
    linked_server_sql = "[" + linked_server.replace("]", "]]" ) + "]"
    return f"""
PRODUTOS AS (
    SELECT COD_SKU, MAX(DESC_SKU) AS DESC_SKU,
           MAX(DESC_GRUPO) AS DESC_GRUPO
    FROM OPENQUERY({linked_server_sql},
        'SELECT COD_SKU, DESC_SKU, DESC_GRUPO FROM ZVIEW_SKU')
    GROUP BY COD_SKU
)
"""


# ----------------------------------------------------------------------------
# Consultas
# ----------------------------------------------------------------------------
SQL_RECORRENTES = """
WITH VENDAS_MES AS (
    SELECT DATEFROMPARTS(YEAR(DATA_CRIACAO), MONTH(DATA_CRIACAO), 1) AS MES,
           LTRIM(RTRIM(SKU)) AS SKU, SUM(QTD_ORIGINAL) AS QTD
    FROM INFOR_DETALHES_PEDIDO
    WHERE FILIAL = %s AND DATA_CRIACAO >= %s AND DATA_CRIACAO < %s
    GROUP BY DATEFROMPARTS(YEAR(DATA_CRIACAO), MONTH(DATA_CRIACAO), 1), LTRIM(RTRIM(SKU))
),
{produtos_cte},
VENDAS_VALIDAS AS (
    SELECT V.* FROM VENDAS_MES V
    LEFT JOIN PRODUTOS P ON P.COD_SKU COLLATE DATABASE_DEFAULT = V.SKU COLLATE DATABASE_DEFAULT
    WHERE UPPER(LTRIM(RTRIM(COALESCE(P.DESC_GRUPO, '')))) NOT IN ('ALMOXARIFADO', 'ESCOLARES', 'INSUMO')
),
RANKING AS (
    SELECT MES, SKU, QTD,
           ROW_NUMBER() OVER (PARTITION BY MES ORDER BY QTD DESC, SKU) AS POSICAO
    FROM VENDAS_VALIDAS
),
TOP_MES AS (SELECT * FROM RANKING WHERE POSICAO <= {top_mensal}),
TOTAIS AS (SELECT SKU, SUM(QTD) AS QTD_PERIODO FROM VENDAS_VALIDAS GROUP BY SKU)
SELECT TOP ({n}) T.SKU, P.DESC_SKU AS DESCRICAO, P.DESC_GRUPO AS CATEGORIA,
       COUNT(*) AS MESES_NO_TOP, MIN(T.POSICAO) AS MELHOR_POSICAO,
       CAST(AVG(T.POSICAO * 1.0) AS DECIMAL(6,2)) AS POSICAO_MEDIA,
       X.QTD_PERIODO
FROM TOP_MES T
JOIN TOTAIS X ON X.SKU = T.SKU
LEFT JOIN PRODUTOS P ON P.COD_SKU COLLATE DATABASE_DEFAULT = T.SKU COLLATE DATABASE_DEFAULT
GROUP BY T.SKU, P.DESC_SKU, P.DESC_GRUPO, X.QTD_PERIODO
ORDER BY COUNT(*) DESC, X.QTD_PERIODO DESC
"""

SQL_MENOS_SAIDOS = """
WITH UNIVERSO AS (
    SELECT LTRIM(RTRIM(SKU)) AS SKU
    FROM INFOR_DETALHES_PEDIDO
    WHERE FILIAL = %s AND SKU IS NOT NULL AND LTRIM(RTRIM(SKU)) <> ''
    GROUP BY LTRIM(RTRIM(SKU))
),
SAIDAS AS (
    SELECT LTRIM(RTRIM(SKU)) AS SKU, SUM(QTD_ORIGINAL) AS QTD_PERIODO
    FROM INFOR_DETALHES_PEDIDO
    WHERE FILIAL = %s AND DATA_CRIACAO >= %s AND DATA_CRIACAO < %s
    GROUP BY LTRIM(RTRIM(SKU))
),
{produtos_cte},
CLASSIFICADOS AS (
    SELECT U.SKU, COALESCE(S.QTD_PERIODO, 0) AS QTD_PERIODO,
           CASE WHEN COALESCE(S.QTD_PERIODO, 0) = 0 THEN 'SEM_SAIDA'
                ELSE 'MENOR_SAIDA' END AS GRUPO,
           P.DESC_SKU AS DESCRICAO, P.DESC_GRUPO AS CATEGORIA
    FROM UNIVERSO U
    LEFT JOIN SAIDAS S ON S.SKU COLLATE DATABASE_DEFAULT = U.SKU COLLATE DATABASE_DEFAULT
    LEFT JOIN PRODUTOS P ON P.COD_SKU COLLATE DATABASE_DEFAULT = U.SKU COLLATE DATABASE_DEFAULT
    WHERE COALESCE(S.QTD_PERIODO, 0) >= 0
      AND UPPER(LTRIM(RTRIM(COALESCE(P.DESC_GRUPO, '')))) NOT IN ('ALMOXARIFADO', 'ESCOLARES','INSUMO')
),
ORDENADOS AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY GRUPO ORDER BY QTD_PERIODO, SKU) AS POSICAO
    FROM CLASSIFICADOS
)
SELECT SKU, DESCRICAO, CATEGORIA, QTD_PERIODO, GRUPO
FROM ORDENADOS WHERE POSICAO <= {n}
ORDER BY CASE WHEN GRUPO = 'MENOR_SAIDA' THEN 0 ELSE 1 END, POSICAO
"""

# Demanda para ruptura: janelas de dias COMPLETOS (terminam ontem), para o dia
# parcial de hoje não derrubar a média.
#   7D   = últimos 7 dias | 3D = últimos 3 dias (o pico continua?)
#   BASE = 28 dias anteriores aos 7 recentes (linha de base)
# VENDA INESPERADA: cada linha de pedido é limitada a um TETO por SKU:
#   teto = maior valor entre (mult x mediana das linhas do SKU nos 35 dias) e (cap_min).
# As colunas *_AJ usam as linhas limitadas ao teto; as demais são as quantidades brutas.
# O pico e a cobertura usam as colunas ajustadas, então uma única venda enorme não
# vira "pico". Cada linha de INFOR_DETALHES_PEDIDO conta como uma linha de pedido do SKU.
SQL_DEMANDA_RUPTURA = """
WITH LINHAS AS (
    SELECT LTRIM(RTRIM(SKU)) AS SKU, DATA_CRIACAO, QTD_ORIGINAL AS QTD,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY QTD_ORIGINAL)
               OVER (PARTITION BY LTRIM(RTRIM(SKU))) AS MEDIANA
    FROM INFOR_DETALHES_PEDIDO
    WHERE FILIAL = %(filial)s
      AND DATA_CRIACAO >= %(ini_base)s AND DATA_CRIACAO < %(fim)s
      AND SKU IS NOT NULL AND LTRIM(RTRIM(SKU)) <> ''
      AND QTD_ORIGINAL > 0
),
COM_TETO AS (
    SELECT SKU, DATA_CRIACAO, QTD,
           CASE WHEN %(mult)s * MEDIANA > %(cap_min)s THEN %(mult)s * MEDIANA
                ELSE %(cap_min)s END AS TETO
    FROM LINHAS
),
COM_AJUSTE AS (
    SELECT SKU, DATA_CRIACAO, QTD, TETO,
           CASE WHEN QTD > TETO THEN TETO ELSE QTD END AS QTD_AJ
    FROM COM_TETO
),
JANELAS AS (
    SELECT SKU, MAX(TETO) AS TETO,
           SUM(CASE WHEN DATA_CRIACAO >= %(ini7)s THEN QTD ELSE 0 END) AS QTD_7D,
           SUM(CASE WHEN DATA_CRIACAO >= %(ini7)s THEN QTD_AJ ELSE 0 END) AS QTD_7D_AJ,
           SUM(CASE WHEN DATA_CRIACAO >= %(ini3)s THEN QTD_AJ ELSE 0 END) AS QTD_3D_AJ,
           SUM(CASE WHEN DATA_CRIACAO < %(ini7)s THEN QTD ELSE 0 END) AS QTD_BASE,
           SUM(CASE WHEN DATA_CRIACAO < %(ini7)s THEN QTD_AJ ELSE 0 END) AS QTD_BASE_AJ,
           MAX(CASE WHEN DATA_CRIACAO >= %(ini7)s THEN QTD ELSE 0 END) AS MAIOR_LINHA_7D
    FROM COM_AJUSTE
    GROUP BY SKU
),
{produtos_cte},
BASE AS (
    SELECT J.SKU, P.DESC_SKU AS DESCRICAO, P.DESC_GRUPO AS CATEGORIA,
           J.QTD_7D, J.QTD_7D_AJ, J.QTD_3D_AJ, J.QTD_BASE, J.QTD_BASE_AJ,
           J.MAIOR_LINHA_7D, J.TETO
    FROM JANELAS J
    LEFT JOIN PRODUTOS P ON P.COD_SKU COLLATE DATABASE_DEFAULT = J.SKU COLLATE DATABASE_DEFAULT
    WHERE J.QTD_7D >= %(min_vol)s
      AND UPPER(LTRIM(RTRIM(COALESCE(P.DESC_GRUPO, '')))) NOT IN ('ALMOXARIFADO', 'ESCOLARES', 'INSUMO')
)
SELECT TOP ({n}) SKU, DESCRICAO, CATEGORIA, QTD_7D, QTD_7D_AJ, QTD_3D_AJ,
       QTD_BASE, QTD_BASE_AJ, MAIOR_LINHA_7D, TETO
FROM BASE
ORDER BY CASE WHEN QTD_7D_AJ >= %(min_vol)s AND QTD_7D_AJ * 4.0 >= %(fator)s * QTD_BASE_AJ THEN 0
              WHEN QTD_7D > QTD_7D_AJ THEN 1
              ELSE 2 END,
         QTD_7D_AJ DESC, SKU
"""

# NFs de entrada pendentes de recebimento: notas com status 'Novo' ou 'No Recebimento'.
# Pendente por SKU = QTD_ESPERADA - QTD_RECEBIDA (só quando positivo).
SQL_NF_PENDENTE = """
SELECT LTRIM(RTRIM(CNF.SKU)) AS SKU,
       SUM(CASE WHEN CNF.QTD_ESPERADA - COALESCE(CNF.QTD_RECEBIDA, 0) > 0
                THEN CNF.QTD_ESPERADA - COALESCE(CNF.QTD_RECEBIDA, 0) ELSE 0 END) AS QTD_PENDENTE,
       COUNT(DISTINCT CASE WHEN CNF.QTD_ESPERADA - COALESCE(CNF.QTD_RECEBIDA, 0) > 0
                           THEN CNF.CHAVE_DE_ACESSO END) AS NOTAS
FROM INFOR_CORPO_NF CNF
JOIN INFOR_CABECALHO_NOTA CN
  ON CN.CHAVE_DE_ACESSO = CNF.CHAVE_DE_ACESSO AND CN.FILIAL = CNF.FILIAL
WHERE CN.STATUS IN ('Novo', 'No Recebimento')
  AND CNF.FILIAL = %(filial)s
  AND CNF.SKU IS NOT NULL
GROUP BY LTRIM(RTRIM(CNF.SKU))
"""


@st.cache_data(ttl=600, show_spinner=False)
def rank_recorrentes(filial, ini, fim, n):
    sql = SQL_RECORRENTES.format(top_mensal=TOP_MENSAL, n=int(n), produtos_cte=sql_produtos_openquery())
    return consultar_sql(sql, (filial, ini, fim))


@st.cache_data(ttl=600, show_spinner=False)
def rank_menos_saidos(filial, ini, fim, n):
    sql = SQL_MENOS_SAIDOS.format(n=int(n), produtos_cte=sql_produtos_openquery())
    return consultar_sql(sql, (filial, filial, ini, fim))


@st.cache_data(ttl=600, show_spinner=False)
def demanda_ruptura(filial, hoje_iso, fator, min_vol, n, mult_atipica, cap_min):
    hoje = date.fromisoformat(hoje_iso)
    params = {
        "filial": filial,
        "fim": core.fmt_sql(hoje),
        "ini7": core.fmt_sql(hoje - timedelta(days=core.JANELA_RECENTE)),
        "ini3": core.fmt_sql(hoje - timedelta(days=core.JANELA_CURTA)),
        "ini_base": core.fmt_sql(hoje - timedelta(days=core.JANELA_RECENTE + core.JANELA_BASE)),
        "min_vol": float(min_vol),
        "fator": float(fator),
        "mult": float(mult_atipica),
        "cap_min": float(cap_min),
    }
    sql = SQL_DEMANDA_RUPTURA.format(n=int(n), produtos_cte=sql_produtos_openquery())
    return consultar_sql(sql, params)


@st.cache_data(ttl=600, show_spinner=False)
def nf_pendentes(filial):
    return consultar_sql(SQL_NF_PENDENTE, {"filial": filial})


# ----------------------------------------------------------------------------
# Análise (botão)
# ----------------------------------------------------------------------------
def _tarefa(sku, media_dia, cfg, dias_cob, modo, ignorar, grupo=None, qtd_periodo=None, descricao=None, categoria=None):
    try:
        _, js = core.buscar_estoque_raw(sku, cfg)
        linhas = core.normalizar_estoque(core.extrair_lista(js), sku, ignorar)
        resultado = core.analisar_estoque(sku, linhas, media_dia, dias_cob, modo)
        resultado["grupo"] = grupo
        resultado["qtd_periodo"] = qtd_periodo
        resultado["descricao"] = descricao
        resultado["categoria"] = categoria
        return resultado
    except PermissionError:
        return {"sku": sku, "erro": True, "status": "Token expirado: clique em Analisar de novo", "token": True}
    except Exception as e:  # noqa: BLE001
        return {"sku": sku, "erro": True, "status": f"Erro: {e}"}


def analisar_um(sku, media_dia, modo, dias_cob, ignorar, grupo=None, qtd_periodo=None, descricao=None, categoria=None):
    cfg = montar_cfg()
    res = _tarefa(sku, media_dia, cfg, dias_cob, modo, ignorar, grupo, qtd_periodo, descricao, categoria)
    if res.get("token"):
        get_token.clear()
    st.session_state.res[modo][sku] = res


def analisar_todos(df, modo, dias_cob, ignorar):
    cfg = montar_cfg()
    barra = st.progress(0.0, text="Consultando estoque...")
    feitos = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {
            ex.submit(
                _tarefa, str(r["SKU"]), float(r["MEDIA_DIA"]), cfg,
                dias_cob, modo, ignorar, r.get("GRUPO"), r.get("QTD_PERIODO"),
                r.get("DESCRICAO"), r.get("CATEGORIA")
            ): str(r["SKU"])
            for _, r in df.iterrows()
        }
        for f in as_completed(futs):
            res = f.result()
            st.session_state.res[modo][futs[f]] = res
            if res.get("token"):
                get_token.clear()
            feitos += 1
            barra.progress(feitos / len(futs), text=f"Consultando estoque... {feitos}/{len(futs)}")
    barra.empty()


def _tarefa_ruptura(linha, cfg, ignorar, limites, fator_pico, pendente,
                    dias_alvo=15, cfg_forn=None, fornecedora=None, min_vol=0):
    sku = str(linha["SKU"])
    try:
        _, js = core.buscar_estoque_raw(sku, cfg)
        linhas = core.normalizar_estoque(core.extrair_lista(js), sku, ignorar)
        demanda = {
            "qtd_7d": linha["QTD_7D"], "qtd_7d_aj": linha["QTD_7D_AJ"],
            "qtd_3d_aj": linha["QTD_3D_AJ"], "qtd_base": linha["QTD_BASE"],
            "qtd_base_aj": linha["QTD_BASE_AJ"], "maior_linha_7d": linha["MAIOR_LINHA_7D"],
        }
        info = pendente.get(sku, {})
        resultado = core.analisar_ruptura(
            sku, linhas, demanda, info.get("qtd", 0.0), limites, fator_pico,
            dias_alvo=dias_alvo, min_vol=min_vol
        )
        resultado["descricao"] = linha.get("DESCRICAO")
        resultado["categoria"] = linha.get("CATEGORIA")
        resultado["notas_pendentes"] = info.get("notas", 0)
        resultado["qtd_7d"] = float(linha["QTD_7D"])

        # Filial abastecedora: só consulta quando há necessidade e o SKU está em ruptura.
        if cfg_forn:
            resultado["fornecedora"] = fornecedora
            precisa = resultado["necessidade"] > 0 and resultado["situacao"] in (
                "Ruptura", "Parcialmente coberta por entrada")
            if precisa:
                try:
                    _, js_f = core.buscar_estoque_raw(sku, cfg_forn)
                    linhas_f = core.normalizar_estoque(core.extrair_lista(js_f), sku)
                    core.avaliar_abastecimento(resultado, sum(l["disp"] for l in linhas_f), fornecedora)
                except PermissionError:
                    raise
                except Exception as e:  # noqa: BLE001
                    resultado.update({"forn_disp": None, "forn_atende": None,
                                      "forn_status": f"Erro na {fornecedora}: {e}"})
            else:
                resultado.update({"forn_disp": None, "forn_atende": None, "forn_status": "—"})
        return resultado
    except PermissionError:
        return {"sku": sku, "erro": True, "status": "Token expirado: carregue de novo", "token": True}
    except Exception as e:  # noqa: BLE001
        return {"sku": sku, "erro": True, "status": f"Erro: {e}"}


def analisar_ruptura_todos(df, pendente, ignorar, limites, fator_pico, dias_alvo, filial, min_vol=0):
    cfg = montar_cfg(filial)
    fornecedora = core.FILIAL_ABASTECEDORA.get(filial)
    cfg_forn = montar_cfg(fornecedora) if fornecedora else None
    barra = st.progress(0.0, text="Consultando estoque dos SKUs em risco...")
    feitos = 0
    resultados = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {
            ex.submit(_tarefa_ruptura, r.to_dict(), cfg, ignorar, limites, fator_pico,
                      pendente, dias_alvo, cfg_forn, fornecedora, min_vol): str(r["SKU"])
            for _, r in df.iterrows()
        }
        for f in as_completed(futs):
            res = f.result()
            resultados[futs[f]] = res
            if res.get("token"):
                get_token.clear()
            feitos += 1
            barra.progress(feitos / len(futs), text=f"Consultando estoque... {feitos}/{len(futs)}")
    barra.empty()
    st.session_state.res["ruptura"] = resultados


# ----------------------------------------------------------------------------
# Telas
# ----------------------------------------------------------------------------
def fmt(v, casas=0):
    try:
        return f"{float(v):,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(v)


def garantir_colunas_cadastro(df, contexto):
    """Evita KeyError com rankings antigos guardados na sessão do Streamlit."""
    df = df.copy()
    df.columns = [str(col).strip().upper() for col in df.columns]
    ausentes = [col for col in ("DESCRICAO", "CATEGORIA") if col not in df.columns]
    for col in ausentes:
        df[col] = ""
    if ausentes:
        st.warning(
            f"O ranking de {contexto} foi carregado antes da inclusão do cadastro "
            "de produtos. Clique em **Carregar ranking** novamente para buscar "
            "Descrição e Categoria na ZVIEW_SKU."
        )
    return df


def render_linhas_ranking(df, modo, infos, larg, dias_cob, ignorar):
    """Lista detalhada do ranking, com analise individual."""
    n = len(infos)
    cab = st.columns(larg)
    for c, (rot, _, _) in zip(cab, infos):
        c.markdown(f"**{rot}**")
    cab[n].markdown("**Ação**")
    cab[n + 1].markdown("**Resultado**")

    for _, r in df.iterrows():
        sku = str(r["SKU"])
        cols = st.columns(larg)
        for c, (_, campo, casas) in zip(cols, infos):
            c.write(sku if campo == "SKU" else (str(r[campo]) if casas is None and pd.notna(r[campo]) else ("—" if casas is None else fmt(r[campo], casas))))
        if cols[n].button("Analisar", key=f"btn_{modo}_{sku}", use_container_width=True):
            with cols[n + 1]:
                with st.spinner("Consultando..."):
                    analisar_um(
                        sku, float(r["MEDIA_DIA"]), modo, dias_cob, ignorar,
                        r.get("GRUPO"), r.get("QTD_PERIODO"),
                        r.get("DESCRICAO"), r.get("CATEGORIA")
                    )
        res = st.session_state.res[modo].get(sku)
        cols[n + 1].write(res["status"] if res else "—")


def render_lista(df, modo, infos, dias_cob, ignorar, filial):
    n = len(infos)
    larg = [1.6] * n + [1.7, 3.4]

    topo = st.columns([1.4, 4])
    if topo[0].button("Analisar todos", key=f"todos_{modo}", type="primary"):
        analisar_todos(df, modo, dias_cob, ignorar)

    if modo == "subir":
        with st.expander(f"Ver lista dos {len(df)} SKUs do ranking", expanded=False):
            render_linhas_ranking(df, modo, infos, larg, dias_cob, ignorar)
    else:
        render_linhas_ranking(df, modo, infos, larg, dias_cob, ignorar)

    # A tabela de resultados permanece FORA do expander e sempre visivel.
    render_resultado(modo, filial)


def _preparar_sugestoes(v, modo, filial):
    """Calcula movimentações propostas sem criar tarefas no WMS."""
    sku = str(v["sku"])
    chave = f"manual_{modo}_{sku}"
    destino = st.session_state.get(chave, "").strip().upper() if modo == "descer" else ""
    ocupacao = st.session_state.get(f"{chave}_ocupacao", 0)
    try:
        propostas = core.planejar_movimentacoes(
            sku, v.get("detalhe", []), v.get("mover", 0), modo, filial,
            v.get("categoria"), destino_manual=destino or None,
            ocupacao_manual=ocupacao,
        )
        return propostas, None
    except ValueError as exc:
        return [], str(exc)


def _tabela_sugestoes(propostas, modo):
    nomes_ranking = {"MENOR_SAIDA": "Menor saída", "SEM_SAIDA": "Sem saída"}
    linhas = []
    for v, t in propostas:
        registro = {
            "SKU": t["sku"],
            "Descrição": v.get("descricao") or "",
            "Categoria": v.get("categoria") or "",
            "Movimentação": "Subir" if modo == "subir" else "Descer",
        }
        if modo == "subir":
            registro["Ranking de origem"] = nomes_ranking.get(v.get("grupo"), "Não identificado")
            registro["Saída nos últimos 30 dias"] = v.get("qtd_periodo", 0)
        registro.update({
            "Sair de": t["fromloc"],
            "ID origem": t.get("fromid", ""),
            "Lote": t.get("lot", ""),
            "Quantidade sugerida": t["qty"],
            "Ir para": t["toloc"],
        })
        linhas.append(registro)
    return pd.DataFrame(linhas)


def _configurar_destinos_manuais(validos, modo, filial):
    """Permite escolher destino na descida se a quantidade não foi plenamente alocada."""
    if modo != "descer":
        return
    incompletos = []
    for v in validos:
        if float(v.get("mover", 0)) <= 0:
            continue
        propostas, erro = _preparar_sugestoes(v, modo, filial)
        planejado = sum(t["qty"] for t in propostas)
        if erro or planejado < float(v["mover"]):
            incompletos.append((v, erro, planejado))
    if not incompletos:
        return
    with st.expander(f"Informar destinos manuais — {len(incompletos)} SKU(s) com pendência", expanded=False):
        st.info("Informe um endereço de chão/RAIA conferido fisicamente e a ocupação atual do SKU. "
                "A capacidade calculada é estimada e não considera outros produtos no endereço.")
        for v, erro, planejado in incompletos:
            sku = str(v["sku"])
            chave = f"manual_{modo}_{sku}"
            st.markdown(f"**{sku} — {v.get('descricao') or ''}**")
            st.caption(erro or f"Sugeridas {planejado:g} de {float(v['mover']):g} peças.")
            a, b = st.columns([2, 1])
            with a:
                st.text_input("Endereço de chão de destino", key=chave,
                              placeholder="RAIA... ou endereço terminado em 000/010")
            with b:
                st.number_input("Peças atuais do SKU neste destino", min_value=0,
                                max_value=1000000, step=1, key=f"{chave}_ocupacao")
        st.caption("As sugestões serão recalculadas automaticamente após informar os dados.")


def painel_sugestoes(validos, modo, filial):
    """Mostra uma linha por deslocamento sugerido, sem qualquer envio ao WMS."""
    propostas = []
    pendencias = []
    for v in validos:
        solicitado = float(v.get("mover", 0))
        if solicitado <= 0:
            continue
        itens, erro = _preparar_sugestoes(v, modo, filial)
        propostas.extend((v, item) for item in itens)
        planejado = sum(item["qty"] for item in itens)
        restante = max(0, solicitado - planejado)
        if erro or restante > 0:
            pendencias.append({
                "SKU": v["sku"], "Descrição": v.get("descricao") or "",
                "Quantidade solicitada": solicitado,
                "Quantidade sugerida": planejado,
                "Quantidade pendente": restante,
                "Motivo": erro or "Sem capacidade conhecida suficiente nos destinos",
            })

    st.markdown("##### Sugestões de movimentação")
    st.caption("Apenas planejamento para conferência e exportação. Nenhuma tarefa é criada no WMS.")
    if propostas:
        tabela = _tabela_sugestoes(propostas, modo)
        c1, c2, c3 = st.columns(3)
        c1.metric("Movimentações sugeridas", len(tabela))
        c2.metric("SKUs com sugestão", tabela["SKU"].nunique())
        c3.metric("Peças sugeridas", fmt(tabela["Quantidade sugerida"].sum()))
        st.dataframe(tabela, hide_index=True, use_container_width=True)
        st.download_button(
            "Exportar sugestões de movimentação (CSV)",
            tabela.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
            file_name=f"sugestoes_movimentacao_{modo}_{date.today():%Y%m%d}.csv",
            mime="text/csv", key=f"csv_sugestoes_{modo}", type="primary",
        )
    else:
        st.info("Nenhuma movimentação pôde ser sugerida com os destinos e saldos conhecidos.")
    if pendencias:
        with st.expander(f"Quantidade sem destino definido — {len(pendencias)} SKU(s)", expanded=False):
            st.dataframe(pd.DataFrame(pendencias), hide_index=True, use_container_width=True)
            st.caption("Quantidades pendentes não estão incluídas no CSV de movimentações.")


def render_resultado(modo, filial):
    resultados = list(st.session_state.res[modo].values())
    if not resultados:
        return

    st.divider()
    st.markdown("#### Resultado da análise")
    erros = [v for v in resultados if v.get("erro")]
    validos = [v for v in resultados if not v.get("erro")]

    # Na aba SUBIR, apenas movimentações efetivamente necessárias entram na tabela.
    # Não listar SKUs sem excesso no chão.
    if modo == "subir":
        validos = [v for v in validos if float(v.get("mover", 0)) > 0]
        validos.sort(key=lambda v: (-float(v["mover"]), str(v["sku"])))
        st.caption("Somente SKUs com excesso disponível no chão, ordenados pela quantidade a retirar.")
    else:
        validos.sort(key=lambda v: (-float(v["mover"]), str(v["sku"])))

    if erros:
        st.warning(f"{len(erros)} SKU(s) não puderam ser analisados. Consulte os avisos na lista acima.")
    if not validos:
        if modo == "subir":
            st.success("Nenhum SKU analisado precisa ser retirado do chão.")
        else:
            st.info("Nenhum resultado válido para mostrar.")
        return

    rot_mover = "Qtd a descer" if modo == "descer" else "Qtd a subir"
    rot_origem = "Sair de (reserva)" if modo == "descer" else "Retirar de (chão)"
    rot_dest = "Ir para (chão)" if modo == "descer" else "Reserva existente (referência)"

    nomes_ranking = {
        "MENOR_SAIDA": "Menor saída",
        "SEM_SAIDA": "Sem saída",
    }
    registros = []
    for v in validos:
        registro = {
            "SKU": v["sku"],
            "Descrição": v.get("descricao") or "",
            "Categoria": v.get("categoria") or "",
        }
        if modo == "subir":
            registro["Ranking de origem"] = nomes_ranking.get(
                v.get("grupo"), "Não identificado"
            )
            registro["Saída nos últimos 30 dias"] = v.get("qtd_periodo", 0)
        registro.update({
            "Status": v["status"],
            "Chão (disp.)": v["chao"],
            "Reserva (disp.)": v["reserva"],
            "Meta no chão": v["meta"],
            rot_mover: v["mover"],
            rot_origem: v["origem"],
            rot_dest: v["destino"],
        })
        registros.append(registro)
    tabela = pd.DataFrame(registros)
    if modo == "subir":
        c1, c2 = st.columns(2)
        c1.metric("SKUs para retirar do chão", len(validos))
        c2.metric("Quantidade total a subir", fmt(tabela[rot_mover].sum()))
        st.caption("Os endereços de reserva são informativos; é necessário validar capacidade e regras de armazenagem antes da movimentação.")

    st.dataframe(tabela, use_container_width=True, hide_index=True)
    _configurar_destinos_manuais(validos, modo, filial)
    painel_sugestoes(validos, modo, filial)

    st.download_button(
        "Baixar CSV",
        tabela.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
        file_name=f"analise_{modo}_{date.today():%Y%m%d}.csv",
        mime="text/csv", key=f"csv_{modo}",
    )
    sel = st.selectbox("Ver endereços de um SKU", [v["sku"] for v in validos], key=f"sel_{modo}")
    detalhe = next(v for v in validos if v["sku"] == sel)["detalhe"]
    if detalhe:
        dados = pd.DataFrame(detalhe).rename(columns={
            "loc": "Endereço", "id": "LPN/ID", "lot": "Lote", "zona": "Zona",
            "qty": "Saldo", "disp": "Disponível", "status": "Status"})
        st.dataframe(dados, use_container_width=True, hide_index=True)
    else:
        st.info("Nenhuma linha de estoque com saldo para esse SKU.")


FAIXA_COR = {"Crítico": "#E24B4A", "Alerta": "#EF9F27", "Atenção": "#D4B106", "OK": "#639922"}
TIPO_ROTULO = {
    "Pico em curso": "🔥 Pico em curso",
    "Pico perdendo força": "📉 Pico perdendo força",
    "Sem histórico": "🆕 Sem histórico",
    "Venda pontual": "⚡ Venda pontual",
    "Normal": "Normal",
}
SITUACAO_ROTULO = {
    "Ruptura": "🚨 Ruptura",
    "Parcialmente coberta por entrada": "🟡 Parcialmente coberta",
    "Coberta por entrada": "✅ Coberta por entrada",
    "Ruptura de picking": "📦 Ruptura de picking",
    "OK": "OK",
}


def _grid_cards(cards):
    """Cartões compactos em grade. Cada item: (título, texto em HTML, cor opcional)."""
    html = "".join(
        f"<div class='guia-card' style='--c:{cor or '#888'}'><b>{titulo}</b>{texto}</div>"
        for titulo, texto, cor in ((c + (None,))[:3] if len(c) == 2 else c for c in cards)
    )
    st.markdown(f"<div class='guia-grid'>{html}</div>", unsafe_allow_html=True)


def _kpis(itens):
    """Indicadores em uma linha. Cada item: (valor, rótulo, cor)."""
    html = "".join(
        f"<div class='kpi' style='--c:{cor}'><b>{valor}</b><span>{rotulo}</span></div>"
        for valor, rotulo, cor in itens
    )
    st.markdown(f"<div class='kpi-grid'>{html}</div>", unsafe_allow_html=True)


def render_guia_ruptura(limites, fator_pico, dias_alvo, fornecedora=None, mult_atipica=5.0, cap_min=10):
    """Explica faixas, tipos de demanda e situações, já com os parâmetros escolhidos."""
    crit, alerta, atencao = (f"{x:g}" for x in limites)
    with st.expander("ℹ️ Como ler este painel", expanded=False):
        st.markdown(
            "**Cobertura (dias) = estoque disponível ÷ saída por dia.** O estoque é o saldo da API "
            "(chão + reserva), sem HOLD e sem o que está alocado. A saída por dia é a maior entre a média dos "
            f"últimos {core.JANELA_RECENTE} dias e a dos {core.JANELA_BASE} dias anteriores."
        )
        st.markdown("**Faixas**")
        _grid_cards([
            ("🔴 Crítico", f"Cobertura <b>menor que {crit} dias</b> ou sem saldo com saída. Ação imediata.", FAIXA_COR["Crítico"]),
            ("🟠 Alerta", f"De <b>{crit} a {alerta} dias</b>. Dá tempo, mas é preciso repor agora.", FAIXA_COR["Alerta"]),
            ("🟡 Atenção", f"De <b>{alerta} a {atencao} dias</b>. Sem reposição, vira Alerta em breve.", FAIXA_COR["Atenção"]),
            ("🟢 OK", f"<b>{atencao} dias ou mais</b> de cobertura.", FAIXA_COR["OK"]),
        ])
        st.markdown("**Tipos de demanda**")
        _grid_cards([
            ("🔥 Pico em curso",
             f"Média de {core.JANELA_RECENTE}d <b>{fator_pico:g}x ou mais</b> a base de {core.JANELA_BASE}d, "
             f"e os últimos {core.JANELA_CURTA}d seguem {core.FATOR_CONTINUA:g}x acima.", "#7F77DD"),
            ("📉 Pico perdendo força",
             f"Teve o aumento, mas os últimos {core.JANELA_CURTA}d voltaram perto da base.", "#7F77DD"),
            ("🆕 Sem histórico",
             f"Não saía nos {core.JANELA_BASE}d anteriores e começou agora. Tratado como pico.", "#7F77DD"),
            ("⚡ Venda pontual",
             "O aumento vinha de uma ou poucas vendas inesperadas muito grandes. Sem elas, não há pico.", "#7F77DD"),
            ("Normal", "Saída dentro do padrão. O risco, se houver, vem do saldo baixo.", "#888"),
        ])
        st.markdown("**Situação do SKU**")
        _grid_cards([
            ("🚨 Ruptura", "Mesmo com a NF pendente, o estoque não cobre a demanda.", "#E24B4A"),
            ("✅ Coberta por entrada", "A NF pendente resolve. Priorize a conferência da nota.", "#1D9E75"),
            ("🟡 Parcialmente coberta", "A NF melhora a cobertura, mas ainda falta reposição.", "#EF9F27"),
            ("📦 Ruptura de picking",
             f"O total dura, mas o chão tem menos de {alerta} dias e há reserva. Descer da reserva.", "#378ADD"),
        ])
        st.markdown(
            f"**Venda inesperada.** Cada linha de pedido é limitada a um teto: o maior valor entre "
            f"**{mult_atipica:g}x a mediana** das linhas do SKU nos últimos "
            f"{core.JANELA_RECENTE + core.JANELA_BASE} dias e **{cap_min:g} peças**. Pico, continuidade e cobertura "
            "usam a saída com esse limite. As colunas *Venda inesperada (7d)* e *Maior pedido 7d* mostram o efeito."
        )
        if fornecedora:
            st.markdown(
                f"**Abastecimento pela {fornecedora}.** Necessidade = saída por dia × {dias_alvo:g} dias − "
                f"(disponível + NF pendente). Só SKUs em ruptura consultam o saldo da {fornecedora}: "
                "*Atende total* cobre tudo, *Atende parcial* cobre uma parte, *Não atende* não tem saldo."
            )


def render_ruptura(limites):
    """Painel de ruptura: indicadores, abas por faixa com tabela compacta e detalhe por SKU."""
    resultados = list(st.session_state.res["ruptura"].values())
    if not resultados:
        return
    meta = st.session_state.dados.get("ruptura_meta", {})

    erros = [v for v in resultados if v.get("erro")]
    validos = [v for v in resultados if not v.get("erro")]
    if erros:
        with st.expander(f"{len(erros)} SKU(s) não puderam ser analisados", expanded=False):
            st.dataframe(pd.DataFrame([{"SKU": e["sku"], "Motivo": e["status"]} for e in erros]),
                         hide_index=True, use_container_width=True)
    if not validos:
        st.info("Nenhum resultado válido para mostrar.")
        return

    forn = next((v.get("fornecedora") for v in validos if v.get("fornecedora")), None)
    col_est = f"Estoque {forn}" if forn else None
    col_ped = f"Pode pedir à {forn}" if forn else None

    registros = []
    for v in validos:
        cob = v["cobertura"]
        cob_e = v["cobertura_entrada"]
        registro = {
            "SKU": v["sku"],
            "Descrição": v.get("descricao") or "",
            "Categoria": v.get("categoria") or "",
            "Faixa": v["faixa"],
            "Situação": v["situacao"],
            "Tipo de demanda": v["tipo"],
            "Cobertura (dias)": None if cob == float("inf") else round(cob, 1),
            "Ruptura prevista": v["data_ruptura"].strftime("%d/%m") if v["data_ruptura"] else "—",
            "Disponível total": v["total"],
            "Chão": v["chao"],
            "Reserva": v["reserva"],
            "Indisponível (HOLD/aloc.)": v["indisponivel"],
            "NF pendente (peças)": v["pendente"],
            "NFs pendentes": v.get("notas_pendentes", 0),
            "Cobertura c/ entrada (dias)": None if cob_e == float("inf") else round(cob_e, 1),
            "Média/dia 7d": round(v["taxa_7d"], 1),
            "Média/dia base": round(v["taxa_base"], 1),
            "Variação (x)": None if v["razao"] is None else round(v["razao"], 2),
            "Venda inesperada (7d)": v["excesso_7d"],
            "Maior pedido 7d": v["maior_linha_7d"],
        }
        if forn:
            registro.update({
                "Necessidade": v["necessidade"],
                col_est: v.get("forn_disp"),
                col_ped: v.get("forn_atende"),
                "Abastecimento": v.get("forn_status", "—"),
            })
        registro["Ação sugerida"] = v["acao"]
        registro["_ordem"] = core.ordem_ruptura(v)
        registros.append(registro)
    tabela = pd.DataFrame(registros).sort_values("_ordem").drop(columns="_ordem").reset_index(drop=True)

    # ---- Indicadores (duas linhas compactas)
    por_faixa = tabela["Faixa"].value_counts()
    _kpis([
        (int(por_faixa.get(f, 0)), f"{f}", FAIXA_COR[f]) for f in ("Crítico", "Alerta", "Atenção", "OK")
    ])
    risco_pico = int(((tabela["Tipo de demanda"].isin(["Pico em curso", "Sem histórico"]))
                      & (tabela["Situação"] != "OK")).sum())
    linha2 = [
        (risco_pico, "Pico em curso com risco", "#7F77DD"),
        (int((tabela["Venda inesperada (7d)"] > 0).sum()), "Com venda inesperada (7d)", "#7F77DD"),
        (int((tabela["Situação"] == "Coberta por entrada").sum()), "Cobertos por entrada", "#1D9E75"),
        (int((tabela["Situação"] == "Ruptura de picking").sum()), "Ruptura de picking", "#378ADD"),
    ]
    if forn:
        linha2 += [
            (int((tabela["Abastecimento"] == "Atende total").sum()), f"{forn} atende total", "#639922"),
            (int((tabela["Abastecimento"] == "Atende parcial").sum()), f"{forn} atende parcial", "#EF9F27"),
            (int((tabela["Abastecimento"] == "Não atende").sum()), f"{forn} não atende", "#E24B4A"),
        ]
    _kpis(linha2)
    if meta:
        st.caption(
            f"{meta.get('triados', 0)} SKUs triados · {meta.get('skus_nf', 0)} com NF pendente de recebimento "
            f"(Novo ou No Recebimento)."
        )

    # ---- Filtros em uma linha
    f1, f2, f3, f4 = st.columns([2, 2, 2, 1.2])
    situacoes = sorted(tabela["Situação"].unique())
    situacoes_sel = f1.multiselect("Situação", situacoes, default=situacoes)
    tipos = sorted(tabela["Tipo de demanda"].unique())
    tipos_sel = f2.multiselect("Tipo de demanda", tipos, default=tipos)
    cats = sorted(c for c in tabela["Categoria"].unique() if c)
    cats_sel = f3.multiselect("Categoria", cats, default=cats)
    todas_colunas = f4.checkbox("Todas as colunas", value=False)

    filtrada = tabela[
        tabela["Situação"].isin(situacoes_sel)
        & tabela["Tipo de demanda"].isin(tipos_sel)
        & (tabela["Categoria"].isin(cats_sel) | (tabela["Categoria"] == ""))
    ]

    compactas = ["SKU", "Descrição", "Tipo de demanda", "Situação", "Cobertura (dias)", "Ruptura prevista",
                 "Disponível total", "NF pendente (peças)"]
    if forn:
        compactas += ["Necessidade", col_est, "Abastecimento"]
    compactas += ["Ação sugerida"]

    config_base = {
        "SKU": st.column_config.TextColumn("SKU", width="small"),
        "Descrição": st.column_config.TextColumn("Descrição", width="medium"),
        "Tipo de demanda": st.column_config.TextColumn("Demanda", width="small"),
        "Situação": st.column_config.TextColumn("Situação", width="small"),
        "Cobertura (dias)": st.column_config.ProgressColumn(
            "Cobertura", min_value=0.0, max_value=float(limites[2]), format="%.1f d",
            help=f"Barra cheia = {limites[2]:g} dias ou mais"),
        "Ruptura prevista": st.column_config.TextColumn("Ruptura", width="small"),
        "Disponível total": st.column_config.NumberColumn("Disponível", format="%d"),
        "NF pendente (peças)": st.column_config.NumberColumn("NF pend.", format="%d"),
        "Necessidade": st.column_config.NumberColumn("Necessid.", format="%d"),
        "Ação sugerida": st.column_config.TextColumn("Ação", width="large"),
    }
    if forn:
        config_base[col_est] = st.column_config.NumberColumn(f"Est. {forn}", format="%d")
        config_base["Abastecimento"] = st.column_config.TextColumn("Abastecimento", width="small")

    def mostrar(parte):
        exibir = parte.drop(columns="Faixa").copy()
        exibir["Tipo de demanda"] = exibir["Tipo de demanda"].map(lambda x: TIPO_ROTULO.get(x, x))
        exibir["Situação"] = exibir["Situação"].map(lambda x: SITUACAO_ROTULO.get(x, x))
        if not todas_colunas:
            exibir = exibir[[c for c in compactas if c in exibir.columns]]
        config = {k: v for k, v in config_base.items() if k in exibir.columns}
        st.dataframe(exibir, use_container_width=True, hide_index=True, column_config=config,
                     height=min(42 + 35 * len(exibir), 430))

    # ---- Uma aba por faixa
    nomes = (("Crítico", "🔴"), ("Alerta", "🟠"), ("Atenção", "🟡"), ("OK", "🟢"))
    abas = st.tabs([f"{ico} {nome} ({int((filtrada['Faixa'] == nome).sum())})" for nome, ico in nomes])
    for aba, (nome, _) in zip(abas, nomes):
        with aba:
            parte = filtrada[filtrada["Faixa"] == nome]
            if parte.empty:
                st.caption("Nenhum SKU nesta faixa.")
            else:
                mostrar(parte)

    st.download_button(
        "Baixar CSV da ruptura",
        filtrada.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
        file_name=f"ruptura_{date.today():%Y%m%d}.csv",
        mime="text/csv", key="csv_ruptura", type="primary",
    )

    # ---- Detalhe do SKU
    if len(filtrada):
        st.markdown("##### Detalhe do SKU")
        sel = st.selectbox("SKU", list(filtrada["SKU"]), key="sel_ruptura", label_visibility="collapsed")
        v = next(x for x in validos if x["sku"] == sel)
        cor = FAIXA_COR[v["faixa"]]
        st.caption(f"{v.get('descricao') or ''} · {v['faixa']} · {SITUACAO_ROTULO.get(v['situacao'], v['situacao'])} · "
                   f"{TIPO_ROTULO.get(v['tipo'], v['tipo'])}")
        itens = [
            (fmt(v["total"]), "Disponível", cor),
            (fmt(v["chao"]), "Chão", "#888"),
            (fmt(v["reserva"]), "Reserva", "#888"),
            (fmt(v["indisponivel"]), "Indisponível (HOLD/aloc.)", "#888"),
            (fmt(v["pendente"]), "NF pendente", "#1D9E75"),
            ("—" if v["cobertura"] == float("inf") else f"{v['cobertura']:.1f} d", "Cobertura", cor),
            (v["data_ruptura"].strftime("%d/%m") if v["data_ruptura"] else "—", "Ruptura prevista", cor),
        ]
        if forn:
            itens.append((fmt(v.get("necessidade", 0)), "Necessidade", "#7F77DD"))
        _kpis(itens)
        st.markdown(f"**Ação:** {v['acao']}")
        d1, d2 = st.columns([1, 2])
        with d1:
            st.bar_chart(pd.DataFrame(
                {"Peças/dia": [v["taxa_base"], v["taxa_7d"], v["taxa_3d"]]},
                index=[f"Base {core.JANELA_BASE}d", f"Últimos {core.JANELA_RECENTE}d", f"Últimos {core.JANELA_CURTA}d"],
            ), height=190)
            st.caption("Pico continua? " + ("Sim." if v["continua"] else "Não.")
                       + (f" Venda inesperada: {fmt(v['excesso_7d'])} peças descontadas." if v["excesso_7d"] > 0 else ""))
        with d2:
            if v["detalhe"]:
                dados = pd.DataFrame(v["detalhe"]).rename(columns={
                    "loc": "Endereço", "id": "LPN/ID", "lot": "Lote", "zona": "Zona",
                    "qty": "Saldo", "disp": "Disponível", "status": "Status"})
                st.dataframe(dados, use_container_width=True, hide_index=True, height=min(42 + 35 * len(dados), 230))
            else:
                st.info("Nenhuma linha de estoque com saldo (RAIA/01) para esse SKU.")


# ----------------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("Parâmetros")
    filial = st.text_input("Filial", value="011324", key="filial")
    try:
        _dados_filial = core.dados_filial(filial)
        st.caption(f"Storerkey: {_dados_filial['storerkey']} | Owner da API: {_dados_filial['owner']}")
    except ValueError as _erro_filial:
        st.error(str(_erro_filial))
    dias_cob = st.number_input(
        "Cobertura desejada no chão (dias de saída)",
        min_value=1,
        max_value=60,
        value=7,
        help="Meta do chão = média diária de saída x esses dias.",
    )
    ignorar = [x for x in st.text_input("Ignorar endereços (separe por vírgula)", value="").split(",") if x.strip()]
    st.divider()
    top_n = st.number_input("Quantos SKUs analisar (mais saídos)", min_value=3, max_value=50, value=10)
    menos_n = st.number_input("Quantos SKUs analisar (menos saídos)", min_value=5, max_value=100, value=100)


    with st.expander("Testar API de estoque"):
        sku_teste = st.text_input("SKU para teste", key="sku_teste")
        if st.button("Chamar API", key="btn_teste") and sku_teste.strip():
            try:
                status, js = core.buscar_estoque_raw(sku_teste.strip(), montar_cfg())
                lista = core.extrair_lista(js)
                st.write(f"HTTP {status}: {len(lista)} linha(s) na resposta")
                st.json(lista[:3] if lista else js)
            except Exception as e:  # noqa: BLE001
                st.error(str(e))

st.title("Giro x Endereçamento")
st.caption("Compara o que mais e o que menos sai com o saldo no chão e na reserva do Infor WMS.")

tab_descer, tab_subir, tab_ruptura = st.tabs(
    ["Descer (mais saem)", "Subir (menos saem)", "Ruptura"]
)
hoje = date.today()

# ---------------- Descer ----------------
with tab_descer:
    ini, fim = core.periodo_meses_fechados(hoje, MESES_RANK)
    ultimo_dia = fim - timedelta(days=1)
    st.write(
        f"SKUs que mais se repetem no top {TOP_MENSAL} mensal, de **{ini:%d/%m/%Y}** a **{ultimo_dia:%d/%m/%Y}** "
        f"({MESES_RANK} meses fechados)."
    )
    if st.button("Carregar ranking", key="carregar_descer"):
        try:
            with st.spinner("Consultando o banco..."):
                df = rank_recorrentes(filial.strip(), core.fmt_sql(ini), core.fmt_sql(fim), top_n)
            df["SKU"] = df["SKU"].astype(str).str.strip()
            dias = (fim - ini).days
            df["MEDIA_DIA"] = df["QTD_PERIODO"].astype(float) / dias
            st.session_state.dados["descer"] = df
            st.session_state.res["descer"] = {}
        except Exception as e:  # noqa: BLE001
            st.error(f"Erro na consulta: {e}")

    df = st.session_state.dados.get("descer")
    if df is not None:
        if df.empty:
            st.warning("Nenhum SKU encontrado para o período.")
        else:
            df = garantir_colunas_cadastro(df, "descida")
            render_lista(
                df,
                "descer",
                [
                    ("SKU", "SKU", 0),
                    ("Descrição", "DESCRICAO", None),
                    ("Categoria", "CATEGORIA", None),
                    ("Meses no top 10", "MESES_NO_TOP", 0),
                    ("Melhor posição", "MELHOR_POSICAO", 0),
                    ("Qtd no período", "QTD_PERIODO", 0),
                    ("Média/dia", "MEDIA_DIA", 1),
                ],
                dias_cob,
                ignorar,
                filial,
            )

# ---------------- Subir ----------------
with tab_subir:
    ini2, fim2 = core.periodo_ultimos_dias(hoje, 30)
    st.write(
        f"Dois rankings independentes de **{ini2:%d/%m/%Y}** a "
        f"**{(fim2 - timedelta(days=1)):%d/%m/%Y}**: "
        f"até **{menos_n} SKUs com menor saída positiva** e até "
        f"**{menos_n} SKUs sem nenhuma saída**."
    )
    st.caption(
        "Os SKUs sem saída são identificados pelo histórico de pedidos da filial. "
        "Produtos que nunca tiveram pedidos não fazem parte dessa base."
    )
    if st.button("Carregar ranking", key="carregar_subir"):
        try:
            with st.spinner("Consultando os dois rankings no banco..."):
                ranking = rank_menos_saidos(
                    filial.strip(), core.fmt_sql(ini2), core.fmt_sql(fim2), menos_n
                )
            ranking["SKU"] = ranking["SKU"].astype(str).str.strip()
            ranking["QTD_PERIODO"] = pd.to_numeric(
                ranking["QTD_PERIODO"], errors="coerce"
            ).fillna(0)
            ranking["MEDIA_DIA"] = ranking["QTD_PERIODO"] / (fim2 - ini2).days
            st.session_state.dados["subir_menor"] = ranking[
                ranking["GRUPO"] == "MENOR_SAIDA"
            ].copy()
            st.session_state.dados["subir_zero"] = ranking[
                ranking["GRUPO"] == "SEM_SAIDA"
            ].copy()
            st.session_state.res["subir"] = {}
        except Exception as e:  # noqa: BLE001
            st.error(f"Erro na consulta: {e}")

    df_menor = st.session_state.dados.get("subir_menor")
    df_zero = st.session_state.dados.get("subir_zero")
    if df_menor is not None and df_zero is not None:
        df_menor = garantir_colunas_cadastro(df_menor, "menor saída")
        df_zero = garantir_colunas_cadastro(df_zero, "sem saída")
        ranking_total = pd.concat([df_menor, df_zero], ignore_index=True)
        if ranking_total.empty:
            st.warning("Nenhum SKU encontrado para os dois rankings.")
        else:
            if st.button("Analisar todos os dois rankings", key="todos_subir", type="primary"):
                analisar_todos(ranking_total, "subir", dias_cob, ignorar)

            campos_subir = [
                ("SKU", "SKU", 0),
                ("Descrição", "DESCRICAO", None),
                ("Categoria", "CATEGORIA", None),
                ("Qtd no período", "QTD_PERIODO", 0),
                ("Média/dia", "MEDIA_DIA", 2),
            ]
            larg_subir = [1.6] * len(campos_subir) + [1.7, 3.4]
            with st.expander(
                f"Ranking 1 — Menor saída nos últimos 30 dias ({len(df_menor)} SKUs)",
                expanded=False,
            ):
                if df_menor.empty:
                    st.info("Não há SKUs com saída positiva neste ranking.")
                else:
                    render_linhas_ranking(
                        df_menor, "subir", campos_subir, larg_subir, dias_cob, ignorar
                    )
            with st.expander(
                f"Ranking 2 — Nenhuma saída nos últimos 30 dias ({len(df_zero)} SKUs)",
                expanded=False,
            ):
                if df_zero.empty:
                    st.info("Não há SKUs sem saída neste ranking.")
                else:
                    render_linhas_ranking(
                        df_zero, "subir", campos_subir, larg_subir, dias_cob, ignorar
                    )

            # Resultado consolidado, fora dos dois painéis recolhíveis.
            render_resultado("subir", filial)

# ---------------- Ruptura ----------------
with tab_ruptura:
    ate = hoje - timedelta(days=1)
    _forn_cod = core.FILIAL_ABASTECEDORA.get(filial.strip())
    st.caption(
        f"Risco de ruptura, com destaque para picos recentes de saída. Saída dos últimos {core.JANELA_RECENTE} dias "
        f"completos (até {ate:%d/%m/%Y}) contra a base de {core.JANELA_BASE} dias · saldo pela API do WMS · "
        f"NF pendente pelo SQL" + (f" · abastecimento pela {_forn_cod}." if _forn_cod else ".")
    )

    with st.expander("Parâmetros da ruptura", expanded=False):
        p1, p2, p3 = st.columns(3)
        fator_pico = p1.slider(
            "Pico = saída recente ≥ X vezes a base", min_value=1.2, max_value=4.0, value=1.5, step=0.1
        )
        min_vol = p2.number_input(
            "Saída mínima nos últimos 7 dias (peças)", min_value=1, max_value=10000, value=20,
            help="Evita alarme em SKU de saída muito baixa."
        )
        triagem = p3.number_input(
            "Quantos SKUs consultar na API", min_value=10, max_value=300, value=80,
            help="Entram primeiro os SKUs em pico, depois os de maior saída recente."
        )
        q1, q2, q3 = st.columns(3)
        lim_crit = q1.number_input("Crítico: cobertura menor que (dias)", min_value=1, max_value=30, value=3)
        lim_alerta = q2.number_input("Alerta: cobertura menor que (dias)", min_value=2, max_value=60, value=7)
        lim_atencao = q3.number_input("Atenção: cobertura menor que (dias)", min_value=3, max_value=90, value=15)
        dias_alvo = st.number_input(
            "Cobertura alvo para pedir à filial abastecedora (dias)", min_value=1, max_value=90, value=15,
            help="Necessidade = média diária x esses dias - (disponível + NF pendente).",
        )
        v1, v2 = st.columns(2)
        mult_atipica = v1.number_input(
            "Venda inesperada: linha de pedido maior que X vezes a mediana do SKU",
            min_value=2.0, max_value=50.0, value=5.0, step=0.5,
            help="Linhas acima do teto são limitadas a ele no cálculo do pico e da cobertura.",
        )
        cap_min = v2.number_input(
            "Venda inesperada: tamanho mínimo do teto (peças)", min_value=1, max_value=10000, value=10,
            help="Evita tratar como atípico um pedido pequeno em SKU de saída muito miúda.",
        )

    limites = (float(lim_crit), float(lim_alerta), float(lim_atencao))
    if not (limites[0] < limites[1] < limites[2]):
        st.warning("Os limites precisam ser crescentes: Crítico < Alerta < Atenção.")
    elif st.button("Carregar e analisar ruptura", key="carregar_ruptura", type="primary"):
        try:
            with st.spinner("Consultando saídas recentes no banco..."):
                dem = demanda_ruptura(filial.strip(), hoje.isoformat(), fator_pico, min_vol, triagem,
                                      mult_atipica, cap_min)
            dem["SKU"] = dem["SKU"].astype(str).str.strip()
            for col in ("QTD_7D", "QTD_7D_AJ", "QTD_3D_AJ", "QTD_BASE", "QTD_BASE_AJ", "MAIOR_LINHA_7D", "TETO"):
                dem[col] = pd.to_numeric(dem[col], errors="coerce").fillna(0)
            for col in ("DESCRICAO", "CATEGORIA"):
                dem[col] = dem[col].fillna("")
        except Exception as e:  # noqa: BLE001
            st.error(f"Erro na consulta de saídas: {e}")
            dem = None

        if dem is not None:
            pendente = {}
            try:
                with st.spinner("Consultando NFs de entrada pendentes..."):
                    nf = nf_pendentes(filial.strip())
                nf["SKU"] = nf["SKU"].astype(str).str.strip()
                for _, r in nf.iterrows():
                    pendente[r["SKU"]] = {
                        "qtd": float(pd.to_numeric(r["QTD_PENDENTE"], errors="coerce") or 0),
                        "notas": int(r["NOTAS"]),
                    }
            except Exception as e:  # noqa: BLE001
                st.warning(
                    "Não consegui consultar as NFs de entrada pendentes; a análise segue "
                    f"sem considerar entradas. Detalhe: {e}"
                )
            st.session_state.dados["ruptura_meta"] = {
                "triados": len(dem), "skus_nf": len(pendente),
            }
            if dem.empty:
                st.session_state.res["ruptura"] = {}
                st.warning("Nenhum SKU com saída suficiente nos últimos 7 dias.")
            else:
                analisar_ruptura_todos(dem, pendente, ignorar, limites, fator_pico, dias_alvo, filial.strip(), min_vol)

    render_guia_ruptura(limites, fator_pico, dias_alvo, core.FILIAL_ABASTECEDORA.get(filial.strip()),
                        mult_atipica, cap_min)
    render_ruptura(limites)
