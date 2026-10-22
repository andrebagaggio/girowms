from datetime import date
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Giro x Endereçamento - Demonstração", layout="wide")

st.markdown("""
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
""", unsafe_allow_html=True)

st.session_state.setdefault("demo_resultados", {"descer": {}, "subir": {}})

FAIXA_COR = {"Crítico": "#E24B4A", "Alerta": "#EF9F27", "Atenção": "#D4B106", "OK": "#639922"}
TIPO_ROTULO = {
    "Pico em curso": "🔥 Pico em curso", "Pico perdendo força": "📉 Pico perdendo força",
    "Sem histórico": "🆕 Sem histórico", "Venda pontual": "⚡ Venda pontual", "Normal": "Normal"
}
SITUACAO_ROTULO = {
    "Ruptura": "🚨 Ruptura", "Parcialmente coberta por entrada": "🟡 Parcialmente coberta",
    "Coberta por entrada": "✅ Coberta por entrada", "Ruptura de picking": "📦 Ruptura de picking", "OK": "OK"
}

def fmt(v, casas=0):
    try:
        return f"{float(v):,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(v)

def _kpis(itens):
    html = "".join(
        f"<div class='kpi' style='--c:{cor}'><b>{valor}</b><span>{rotulo}</span></div>"
        for valor, rotulo, cor in itens
    )
    st.markdown(f"<div class='kpi-grid'>{html}</div>", unsafe_allow_html=True)

def _grid_cards(cards):
    html = "".join(
        f"<div class='guia-card' style='--c:{cor}'><b>{titulo}</b>{texto}</div>"
        for titulo, texto, cor in cards
    )
    st.markdown(f"<div class='guia-grid'>{html}</div>", unsafe_allow_html=True)

def criar_ranking_descer():
    return pd.DataFrame([
        {"SKU":"SKU-1001","DESCRICAO":"Arroz tipo 1 5kg","CATEGORIA":"Mercearia","MESES_NO_TOP":3,"MELHOR_POSICAO":1,"QTD_PERIODO":1840,"MEDIA_DIA":20.4},
        {"SKU":"SKU-1002","DESCRICAO":"Feijão carioca 1kg","CATEGORIA":"Mercearia","MESES_NO_TOP":3,"MELHOR_POSICAO":2,"QTD_PERIODO":1260,"MEDIA_DIA":14.0},
        {"SKU":"SKU-1003","DESCRICAO":"Óleo de soja 900ml","CATEGORIA":"Mercearia","MESES_NO_TOP":2,"MELHOR_POSICAO":4,"QTD_PERIODO":990,"MEDIA_DIA":11.0},
        {"SKU":"SKU-1004","DESCRICAO":"Detergente neutro 500ml","CATEGORIA":"Limpeza","MESES_NO_TOP":2,"MELHOR_POSICAO":6,"QTD_PERIODO":720,"MEDIA_DIA":8.0},
        {"SKU":"SKU-1005","DESCRICAO":"Papel toalha pacote","CATEGORIA":"Higiene","MESES_NO_TOP":1,"MELHOR_POSICAO":9,"QTD_PERIODO":450,"MEDIA_DIA":5.0},
    ])

def criar_ranking_subir():
    return pd.DataFrame([
        {"SKU":"SKU-2001","DESCRICAO":"Molho de tomate 300g","CATEGORIA":"Mercearia","QTD_PERIODO":8,"MEDIA_DIA":0.27,"GRUPO":"MENOR_SAIDA"},
        {"SKU":"SKU-2002","DESCRICAO":"Biscoito água e sal","CATEGORIA":"Biscoitos","QTD_PERIODO":12,"MEDIA_DIA":0.40,"GRUPO":"MENOR_SAIDA"},
        {"SKU":"SKU-2003","DESCRICAO":"Amaciante 2L","CATEGORIA":"Limpeza","QTD_PERIODO":18,"MEDIA_DIA":0.60,"GRUPO":"MENOR_SAIDA"},
        {"SKU":"SKU-2004","DESCRICAO":"Copo descartável 200ml","CATEGORIA":"Descartáveis","QTD_PERIODO":0,"MEDIA_DIA":0.00,"GRUPO":"SEM_SAIDA"},
        {"SKU":"SKU-2005","DESCRICAO":"Guardanapo pacote","CATEGORIA":"Descartáveis","QTD_PERIODO":0,"MEDIA_DIA":0.00,"GRUPO":"SEM_SAIDA"},
        {"SKU":"SKU-2006","DESCRICAO":"Esponja multiuso","CATEGORIA":"Limpeza","QTD_PERIODO":0,"MEDIA_DIA":0.00,"GRUPO":"SEM_SAIDA"},
    ])

def analisar_demo(row, modo, filial, dias_cob, ignorar):
    sku = str(row["SKU"])
    if modo == "descer":
        mover = max(0, int(row["MEDIA_DIA"] * dias_cob - 18))
        status = "Excesso no chão" if mover else "Dentro da meta"
        chao, reserva, origem, destino = 42 + int(row["MEDIA_DIA"]), 180, "RES-A01", "RAIA-01"
    else:
        mover = 35 if row["GRUPO"] == "SEM_SAIDA" else 18
        status = "Excesso no chão" if mover else "Dentro da meta"
        chao, reserva, origem, destino = 55, 120, "RAIA-02", "RES-B03"
    resultado = {
        "sku": sku, "descricao": row.get("DESCRICAO",""), "categoria": row.get("CATEGORIA",""),
        "grupo": row.get("GRUPO"), "qtd_periodo": row.get("QTD_PERIODO",0),
        "status": status, "chao": chao, "reserva": reserva, "meta": int(row["MEDIA_DIA"]*dias_cob),
        "mover": mover, "origem": origem, "destino": destino,
        "detalhe": [
            {"loc": origem, "id": f"LPN-{sku[-4:]}01", "lot":"L2409", "zona":"RESERVA","qty":reserva,"disp":reserva,"status":"OK"},
            {"loc": destino, "id": f"LPN-{sku[-4:]}02", "lot":"L2408", "zona":"CHAO","qty":chao,"disp":chao,"status":"OK"},
        ]
    }
    st.session_state.demo_resultados[modo][sku] = resultado

def render_linhas_ranking(df, modo, infos, dias_cob, ignorar):
    cab = st.columns([1.6] * len(infos) + [1.7, 3.4])
    for c, (rot, _, _) in zip(cab, infos):
        c.markdown(f"**{rot}**")
    cab[len(infos)].markdown("**Ação**")
    cab[len(infos)+1].markdown("**Resultado**")
    for _, r in df.iterrows():
        cols = st.columns([1.6] * len(infos) + [1.7, 3.4])
        for c, (_, campo, casas) in zip(cols, infos):
            valor = r.get(campo, "")
            if campo == "SKU":
                c.write(str(valor))
            elif casas is None:
                c.write(str(valor) if pd.notna(valor) and valor != "" else "—")
            else:
                c.write(fmt(valor, casas))
        if cols[len(infos)].button("Analisar", key=f"analisar_{modo}_{r['SKU']}", use_container_width=True):
            analisar_demo(r, modo, st.session_state.get("filial","011324"), dias_cob, ignorar)
        res = st.session_state.demo_resultados[modo].get(str(r["SKU"]))
        cols[len(infos)+1].write(res["status"] if res else "—")

def painel_sugestoes(validos, modo):
    linhas = []
    for v in validos:
        if v["mover"] <= 0:
            continue
        linhas.append({
            "SKU":v["sku"],"Descrição":v.get("descricao",""),"Categoria":v.get("categoria",""),
            "Movimentação":"Descer" if modo=="descer" else "Subir",
            "Sair de":v["origem"],"ID origem":v["detalhe"][0]["id"],"Lote":v["detalhe"][0]["lot"],
            "Quantidade sugerida":v["mover"],"Ir para":v["destino"]
        })
    st.markdown("##### Sugestões de movimentação")
    st.caption("Demonstração fictícia: nenhuma tarefa é criada no WMS.")
    if linhas:
        tabela = pd.DataFrame(linhas)
        a,b,c = st.columns(3)
        a.metric("Movimentações sugeridas",len(tabela))
        b.metric("SKUs com sugestão",tabela["SKU"].nunique())
        c.metric("Peças sugeridas",fmt(tabela["Quantidade sugerida"].sum()))
        st.dataframe(tabela, hide_index=True, use_container_width=True)
        st.download_button("Exportar sugestões (CSV)", tabela.to_csv(index=False,sep=";",decimal=",").encode("utf-8-sig"),
                           file_name=f"demo_sugestoes_{modo}.csv",mime="text/csv",key=f"csv_sug_{modo}")
    else:
        st.info("Nenhuma movimentação fictícia necessária.")

def render_resultado(modo, filial):
    resultados = list(st.session_state.demo_resultados[modo].values())
    if not resultados:
        return
    st.divider()
    st.markdown("#### Resultado da análise")
    validos = [v for v in resultados if modo != "subir" or v["mover"] > 0]
    validos.sort(key=lambda v:(-float(v["mover"]),str(v["sku"])))
    if not validos:
        st.info("Nenhum resultado para mostrar.")
        return
    rot_mover = "Qtd a descer" if modo=="descer" else "Qtd a subir"
    registros=[]
    for v in validos:
        reg={"SKU":v["sku"],"Descrição":v["descricao"],"Categoria":v["categoria"],"Status":v["status"],
             "Chão (disp.)":v["chao"],"Reserva (disp.)":v["reserva"],"Meta no chão":v["meta"],
             rot_mover:v["mover"],"Sair de (reserva)" if modo=="descer" else "Retirar de (chão)":v["origem"],
             "Ir para (chão)" if modo=="descer" else "Reserva existente (referência)":v["destino"]}
        if modo=="subir":
            reg["Ranking de origem"] = "Sem saída" if v.get("grupo")=="SEM_SAIDA" else "Menor saída"
            reg["Saída nos últimos 30 dias"] = v.get("qtd_periodo",0)
        registros.append(reg)
    tabela=pd.DataFrame(registros)
    if modo=="subir":
        a,b=st.columns(2); a.metric("SKUs para retirar do chão",len(validos)); b.metric("Quantidade total a subir",fmt(tabela[rot_mover].sum()))
    st.dataframe(tabela,hide_index=True,use_container_width=True)
    painel_sugestoes(validos,modo)
    st.download_button("Baixar análise CSV",tabela.to_csv(index=False,sep=";",decimal=",").encode("utf-8-sig"),
                       file_name=f"demo_analise_{modo}.csv",mime="text/csv",key=f"csv_resultado_{modo}")
    sel=st.selectbox("Ver endereços de um SKU",[v["sku"] for v in validos],key=f"detalhe_{modo}")
    det=next(v for v in validos if v["sku"]==sel)["detalhe"]
    st.dataframe(pd.DataFrame(det).rename(columns={"loc":"Endereço","id":"LPN/ID","lot":"Lote","zona":"Zona","qty":"Saldo","disp":"Disponível","status":"Status"}),
                 hide_index=True,use_container_width=True)

def criar_ruptura():
    return pd.DataFrame([
        {"SKU":"SKU-3001","Descrição":"Leite UHT integral 1L","Categoria":"Laticínios","Faixa":"Crítico","Situação":"Ruptura","Tipo de demanda":"Pico em curso","Cobertura (dias)":1.2,"Ruptura prevista":"22/10","Disponível total":24,"Chão":8,"Reserva":16,"Indisponível (HOLD/aloc.)":12,"NF pendente (peças)":10,"NFs pendentes":1,"Cobertura c/ entrada (dias)":1.7,"Média/dia 7d":20.0,"Média/dia base":8.0,"Variação (x)":2.5,"Venda inesperada (7d)":0,"Maior pedido 7d":18,"Necessidade":266,"Estoque Filial 011325":90,"Pode pedir à 011325":"Atende parcial","Abastecimento":"Atende parcial","Ação sugerida":"Repor imediatamente e priorizar recebimento."},
        {"SKU":"SKU-3002","Descrição":"Café torrado 500g","Categoria":"Mercearia","Faixa":"Alerta","Situação":"Parcialmente coberta por entrada","Tipo de demanda":"Pico perdendo força","Cobertura (dias)":5.0,"Ruptura prevista":"26/10","Disponível total":75,"Chão":25,"Reserva":50,"Indisponível (HOLD/aloc.)":5,"NF pendente (peças)":80,"NFs pendentes":2,"Cobertura c/ entrada (dias)":10.3,"Média/dia 7d":15.0,"Média/dia base":11.0,"Variação (x)":1.36,"Venda inesperada (7d)":5,"Maior pedido 7d":30,"Necessidade":95,"Estoque Filial 011325":200,"Pode pedir à 011325":"Atende total","Abastecimento":"Atende total","Ação sugerida":"Conferir a NF pendente e planejar reposição."},
        {"SKU":"SKU-3003","Descrição":"Detergente neutro 500ml","Categoria":"Limpeza","Faixa":"Atenção","Situação":"Ruptura de picking","Tipo de demanda":"Normal","Cobertura (dias)":10.0,"Ruptura prevista":"31/10","Disponível total":200,"Chão":12,"Reserva":188,"Indisponível (HOLD/aloc.)":0,"NF pendente (peças)":0,"NFs pendentes":0,"Cobertura c/ entrada (dias)":10.0,"Média/dia 7d":20.0,"Média/dia base":20.0,"Variação (x)":1.0,"Venda inesperada (7d)":0,"Maior pedido 7d":22,"Necessidade":100,"Estoque Filial 011325":0,"Pode pedir à 011325":"Não atende","Abastecimento":"Não atende","Ação sugerida":"Descer quantidade da reserva para o picking."},
        {"SKU":"SKU-3004","Descrição":"Papel higiênico 12 rolos","Categoria":"Higiene","Faixa":"OK","Situação":"OK","Tipo de demanda":"Normal","Cobertura (dias)":24.0,"Ruptura prevista":"14/11","Disponível total":480,"Chão":120,"Reserva":360,"Indisponível (HOLD/aloc.)":20,"NF pendente (peças)":0,"NFs pendentes":0,"Cobertura c/ entrada (dias)":24.0,"Média/dia 7d":20.0,"Média/dia base":19.0,"Variação (x)":1.05,"Venda inesperada (7d)":0,"Maior pedido 7d":25,"Necessidade":0,"Estoque Filial 011325":150,"Pode pedir à 011325":"—","Abastecimento":"—","Ação sugerida":"Acompanhar normalmente."},
        {"SKU":"SKU-3005","Descrição":"Biscoito recheado 130g","Categoria":"Biscoitos","Faixa":"Crítico","Situação":"Coberta por entrada","Tipo de demanda":"Sem histórico","Cobertura (dias)":0.0,"Ruptura prevista":"21/10","Disponível total":0,"Chão":0,"Reserva":0,"Indisponível (HOLD/aloc.)":0,"NF pendente (peças)":150,"NFs pendentes":1,"Cobertura c/ entrada (dias)":12.0,"Média/dia 7d":12.5,"Média/dia base":0.0,"Variação (x)":None,"Venda inesperada (7d)":0,"Maior pedido 7d":15,"Necessidade":37,"Estoque Filial 011325":45,"Pode pedir à 011325":"Atende total","Abastecimento":"Atende total","Ação sugerida":"Priorizar o recebimento da NF para evitar ruptura."},
        {"SKU":"SKU-3006","Descrição":"Amaciante 2L","Categoria":"Limpeza","Faixa":"Alerta","Situação":"Ruptura","Tipo de demanda":"Venda pontual","Cobertura (dias)":4.0,"Ruptura prevista":"25/10","Disponível total":40,"Chão":10,"Reserva":30,"Indisponível (HOLD/aloc.)":8,"NF pendente (peças)":0,"NFs pendentes":0,"Cobertura c/ entrada (dias)":4.0,"Média/dia 7d":10.0,"Média/dia base":7.0,"Variação (x)":1.43,"Venda inesperada (7d)":25,"Maior pedido 7d":35,"Necessidade":110,"Estoque Filial 011325":30,"Pode pedir à 011325":"Atende parcial","Abastecimento":"Atende parcial","Ação sugerida":"Validar venda atípica e avaliar reposição."},
    ])

def render_ruptura(limites):
    tabela=st.session_state.get("ruptura_demo")
    if tabela is None or tabela.empty:
        return
    tabela=tabela.copy()
    forn="011325"; col_est=f"Estoque Filial {forn}"; col_ped=f"Pode pedir à {forn}"
    por_faixa=tabela["Faixa"].value_counts()
    _kpis([(int(por_faixa.get(f,0)),f,FAIXA_COR[f]) for f in ("Crítico","Alerta","Atenção","OK")])
    risco_pico=int((tabela["Tipo de demanda"].isin(["Pico em curso","Sem histórico"]) & (tabela["Situação"]!="OK")).sum())
    _kpis([
        (risco_pico,"Pico em curso com risco","#7F77DD"),
        (int((tabela["Venda inesperada (7d)"]>0).sum()),"Com venda inesperada (7d)","#7F77DD"),
        (int((tabela["Situação"]=="Coberta por entrada").sum()),"Cobertos por entrada","#1D9E75"),
        (int((tabela["Situação"]=="Ruptura de picking").sum()),"Ruptura de picking","#378ADD"),
        (int((tabela["Abastecimento"]=="Atende total").sum()),f"{forn} atende total","#639922"),
        (int((tabela["Abastecimento"]=="Atende parcial").sum()),f"{forn} atende parcial","#EF9F27"),
        (int((tabela["Abastecimento"]=="Não atende").sum()),f"{forn} não atende","#E24B4A"),
    ])
    st.caption("6 SKUs fictícios triados · 3 SKUs com NF pendente de recebimento.")
    f1,f2,f3,f4=st.columns([2,2,2,1.2])
    situacoes=sorted(tabela["Situação"].unique())
    situacoes_sel=f1.multiselect("Situação",situacoes,default=situacoes)
    tipos=sorted(tabela["Tipo de demanda"].unique())
    tipos_sel=f2.multiselect("Tipo de demanda",tipos,default=tipos)
    cats=sorted(c for c in tabela["Categoria"].unique() if c)
    cats_sel=f3.multiselect("Categoria",cats,default=cats)
    todas=f4.checkbox("Todas as colunas",value=False)
    filtrada=tabela[tabela["Situação"].isin(situacoes_sel)&tabela["Tipo de demanda"].isin(tipos_sel)&tabela["Categoria"].isin(cats_sel)]
    compactas=["SKU","Descrição","Tipo de demanda","Situação","Cobertura (dias)","Ruptura prevista","Disponível total","NF pendente (peças)","Necessidade",col_est,"Abastecimento","Ação sugerida"]
    config={
        "SKU":st.column_config.TextColumn("SKU",width="small"),
        "Descrição":st.column_config.TextColumn("Descrição",width="medium"),
        "Tipo de demanda":st.column_config.TextColumn("Demanda",width="small"),
        "Situação":st.column_config.TextColumn("Situação",width="small"),
        "Cobertura (dias)":st.column_config.ProgressColumn("Cobertura",min_value=0,max_value=float(limites[2]),format="%.1f d"),
        "Ruptura prevista":st.column_config.TextColumn("Ruptura",width="small"),
        "Disponível total":st.column_config.NumberColumn("Disponível",format="%d"),
        "NF pendente (peças)":st.column_config.NumberColumn("NF pend.",format="%d"),
        "Necessidade":st.column_config.NumberColumn("Necessid.",format="%d"),
        col_est:st.column_config.NumberColumn(f"Est. {forn}",format="%d"),
        "Abastecimento":st.column_config.TextColumn("Abastecimento",width="small"),
        "Ação sugerida":st.column_config.TextColumn("Ação",width="large"),
    }
    def mostrar(parte):
        exibir=parte.drop(columns=["Faixa"]).copy()
        exibir["Tipo de demanda"]=exibir["Tipo de demanda"].map(lambda x:TIPO_ROTULO.get(x,x))
        exibir["Situação"]=exibir["Situação"].map(lambda x:SITUACAO_ROTULO.get(x,x))
        if not todas:
            exibir=exibir[[c for c in compactas if c in exibir.columns]]
        st.dataframe(exibir,use_container_width=True,hide_index=True,column_config={k:v for k,v in config.items() if k in exibir.columns},height=min(42+35*len(exibir),430))
    nomes=(("Crítico","🔴"),("Alerta","🟠"),("Atenção","🟡"),("OK","🟢"))
    abas=st.tabs([f"{ico} {nome} ({int((filtrada['Faixa']==nome).sum())})" for nome,ico in nomes])
    for aba,(nome,_) in zip(abas,nomes):
        with aba:
            parte=filtrada[filtrada["Faixa"]==nome]
            if parte.empty: st.caption("Nenhum SKU nesta faixa.")
            else: mostrar(parte)
    st.download_button("Baixar CSV da ruptura",filtrada.to_csv(index=False,sep=";",decimal=",").encode("utf-8-sig"),
                       file_name=f"demo_ruptura_{date.today():%Y%m%d}.csv",mime="text/csv",key="csv_ruptura",type="primary")
    if len(filtrada):
        st.markdown("##### Detalhe do SKU")
        sel=st.selectbox("SKU",list(filtrada["SKU"]),key="sel_ruptura")
        v=tabela[tabela["SKU"]==sel].iloc[0]
        st.caption(f"{v['Descrição']} · {v['Faixa']} · {SITUACAO_ROTULO.get(v['Situação'],v['Situação'])} · {TIPO_ROTULO.get(v['Tipo de demanda'],v['Tipo de demanda'])}")
        _kpis([
            (fmt(v["Disponível total"]),"Disponível",FAIXA_COR[v["Faixa"]]),(fmt(v["Chão"]),"Chão","#888"),
            (fmt(v["Reserva"]),"Reserva","#888"),(fmt(v["Indisponível (HOLD/aloc.)"]),"Indisponível (HOLD/aloc.)","#888"),
            (fmt(v["NF pendente (peças)"]),"NF pendente","#1D9E75"),(f"{v['Cobertura (dias)']:.1f} d","Cobertura",FAIXA_COR[v["Faixa"]]),
            (v["Ruptura prevista"],"Ruptura prevista",FAIXA_COR[v["Faixa"]]),(fmt(v["Necessidade"]),"Necessidade","#7F77DD")
        ])
        st.markdown(f"**Ação:** {v['Ação sugerida']}")
        d1,d2=st.columns([1,2])
        with d1:
            st.bar_chart(pd.DataFrame({"Peças/dia":[v["Média/dia base"],v["Média/dia 7d"],v["Média/dia 7d"]*0.8]},
                                      index=["Base 28d","Últimos 7d","Últimos 3d"]),height=190)
            st.caption("Gráfico demonstrativo de demanda.")
        with d2:
            st.dataframe(pd.DataFrame([
                {"Endereço":"RAIA-01","LPN/ID":f"LPN-{sel[-4:]}01","Lote":"L2409","Zona":"CHAO","Saldo":v["Chão"],"Disponível":v["Chão"],"Status":"OK"},
                {"Endereço":"RES-A01","LPN/ID":f"LPN-{sel[-4:]}02","Lote":"L2408","Zona":"RESERVA","Saldo":v["Reserva"],"Disponível":v["Reserva"],"Status":"OK"},
            ]),hide_index=True,use_container_width=True,height=180)

with st.sidebar:
    st.header("Parâmetros")
    st.caption("Modo demonstração — dados fictícios")
    filial=st.text_input("Filial",value="011324",key="filial")
    st.caption("Storerkey: DEMO | Owner da API: DEMO")
    dias_cob=st.number_input("Cobertura desejada no chão (dias de saída)",min_value=1,max_value=60,value=7)
    ignorar=[x.strip() for x in st.text_input("Ignorar endereços (separe por vírgula)",value="").split(",") if x.strip()]
    st.divider()
    top_n=st.number_input("Quantos SKUs analisar (mais saídos)",min_value=3,max_value=50,value=10)
    menos_n=st.number_input("Quantos SKUs analisar (menos saídos)",min_value=5,max_value=100,value=10)
    with st.expander("Testar API de estoque"):
        st.info("Desativado na demonstração. Nenhuma API será chamada.")
        st.text_input("SKU para teste",key="sku_teste")
        st.button("Chamar API",key="btn_teste",disabled=True)

st.title("Giro x Endereçamento")
st.caption("Demonstração visual — dados fictícios, sem consultas ao banco ou ao Infor WMS.")
tab_descer,tab_subir,tab_ruptura=st.tabs(["Descer (mais saem)","Subir (menos saem)","Ruptura"])

with tab_descer:
    st.write("SKUs que mais se repetem no top 10 mensal, de **01/07/2026** a **30/09/2026** (3 meses fechados).")
    if st.button("Carregar ranking",key="carregar_descer"):
        st.session_state["ranking_descer"]=criar_ranking_descer()
        st.session_state.demo_resultados["descer"]={}
    df=st.session_state.get("ranking_descer")
    if df is not None:
        if df.empty: st.warning("Nenhum SKU encontrado.")
        else:
            st.caption("Dados de exemplo para visualizar o ranking.")
            infos=[("SKU","SKU",0),("Descrição","DESCRICAO",None),("Categoria","CATEGORIA",None),
                   ("Meses no top 10","MESES_NO_TOP",0),("Melhor posição","MELHOR_POSICAO",0),
                   ("Qtd no período","QTD_PERIODO",0),("Média/dia","MEDIA_DIA",1)]
            if st.button("Analisar todos",key="todos_descer",type="primary"):
                for _,r in df.head(int(top_n)).iterrows(): analisar_demo(r,"descer",filial,dias_cob,ignorar)
            render_linhas_ranking(df.head(int(top_n)), "descer", infos,dias_cob,ignorar)
            render_resultado("descer",filial)

with tab_subir:
    st.write("Dois rankings independentes de **21/09/2026** a **20/10/2026**: menor saída positiva e nenhuma saída.")
    st.caption("Os produtos sem saída representam SKUs que já tiveram pedidos no histórico fictício.")
    if st.button("Carregar ranking",key="carregar_subir"):
        ranking=criar_ranking_subir()
        st.session_state["ranking_subir_menor"]=ranking[ranking["GRUPO"]=="MENOR_SAIDA"].copy()
        st.session_state["ranking_subir_zero"]=ranking[ranking["GRUPO"]=="SEM_SAIDA"].copy()
        st.session_state.demo_resultados["subir"]={}
    df_menor=st.session_state.get("ranking_subir_menor")
    df_zero=st.session_state.get("ranking_subir_zero")
    if df_menor is not None and df_zero is not None:
        total=pd.concat([df_menor,df_zero],ignore_index=True)
        if st.button("Analisar todos os dois rankings",key="todos_subir",type="primary"):
            for _,r in total.head(int(menos_n)*2).iterrows(): analisar_demo(r,"subir",filial,dias_cob,ignorar)
        campos=[("SKU","SKU",0),("Descrição","DESCRICAO",None),("Categoria","CATEGORIA",None),("Qtd no período","QTD_PERIODO",0),("Média/dia","MEDIA_DIA",2)]
        with st.expander(f"Ranking 1 — Menor saída nos últimos 30 dias ({len(df_menor)} SKUs)",expanded=False):
            if df_menor.empty: st.info("Não há SKUs com saída positiva.")
            else: render_linhas_ranking(df_menor.head(int(menos_n)),"subir",campos,dias_cob,ignorar)
        with st.expander(f"Ranking 2 — Nenhuma saída nos últimos 30 dias ({len(df_zero)} SKUs)",expanded=False):
            if df_zero.empty: st.info("Não há SKUs sem saída.")
            else: render_linhas_ranking(df_zero.head(int(menos_n)),"subir",campos,dias_cob,ignorar)
        render_resultado("subir",filial)

with tab_ruptura:
    st.caption("Risco de ruptura com dados fictícios · saída dos últimos 7 dias completos contra base de 28 dias · saldo demonstrativo.")
    with st.expander("Parâmetros da ruptura",expanded=False):
        p1,p2,p3=st.columns(3)
        fator_pico=p1.slider("Pico = saída recente ≥ X vezes a base",min_value=1.2,max_value=4.0,value=1.5,step=0.1)
        min_vol=p2.number_input("Saída mínima nos últimos 7 dias (peças)",min_value=1,max_value=10000,value=20)
        triagem=p3.number_input("Quantos SKUs consultar na API",min_value=10,max_value=300,value=80)
        q1,q2,q3=st.columns(3)
        lim_crit=q1.number_input("Crítico: cobertura menor que (dias)",min_value=1,max_value=30,value=3)
        lim_alerta=q2.number_input("Alerta: cobertura menor que (dias)",min_value=2,max_value=60,value=7)
        lim_atencao=q3.number_input("Atenção: cobertura menor que (dias)",min_value=3,max_value=90,value=15)
        dias_alvo=st.number_input("Cobertura alvo para pedir à filial abastecedora (dias)",min_value=1,max_value=90,value=15)
        v1,v2=st.columns(2)
        mult_atipica=v1.number_input("Venda inesperada: linha maior que X vezes a mediana",min_value=2.0,max_value=50.0,value=5.0,step=0.5)
        cap_min=v2.number_input("Venda inesperada: tamanho mínimo do teto (peças)",min_value=1,max_value=10000,value=10)
    limites=(float(lim_crit),float(lim_alerta),float(lim_atencao))
    if not (limites[0]<limites[1]<limites[2]):
        st.warning("Os limites precisam ser crescentes: Crítico < Alerta < Atenção.")
    if st.button("Carregar e analisar ruptura",key="carregar_ruptura",type="primary"):
        st.session_state["ruptura_demo"]=criar_ruptura()
    with st.expander("ℹ️ Como ler este painel",expanded=False):
        st.markdown("**Cobertura (dias) = estoque disponível ÷ saída por dia.** Os números são ilustrativos e não refletem o estoque real.")
        st.markdown("**Faixas**")
        _grid_cards([
            ("🔴 Crítico",f"Cobertura menor que {limites[0]:g} dias. Ação imediata.",FAIXA_COR["Crítico"]),
            ("🟠 Alerta",f"De {limites[0]:g} a {limites[1]:g} dias. Planejar reposição.",FAIXA_COR["Alerta"]),
            ("🟡 Atenção",f"De {limites[1]:g} a {limites[2]:g} dias. Acompanhar.",FAIXA_COR["Atenção"]),
            ("🟢 OK",f"{limites[2]:g} dias ou mais de cobertura.",FAIXA_COR["OK"]),
        ])
        st.markdown("**Tipos de demanda**")
        _grid_cards([
            ("🔥 Pico em curso",f"Saída recente ≥ {fator_pico:g}x a base e demanda curta continua alta.","#7F77DD"),
            ("📉 Pico perdendo força","A demanda subiu, mas começa a voltar ao padrão.","#7F77DD"),
            ("🆕 Sem histórico","Não havia saída na base e começou agora.","#7F77DD"),
            ("⚡ Venda pontual","O aumento veio de uma ou poucas linhas grandes.","#7F77DD"),
            ("Normal","Saída dentro do padrão.","#888"),
        ])
        st.markdown("**Situação do SKU**")
        _grid_cards([
            ("🚨 Ruptura","Estoque insuficiente para a demanda.","#E24B4A"),
            ("✅ Coberta por entrada","A NF pendente cobre a necessidade.","#1D9E75"),
            ("🟡 Parcialmente coberta","A entrada melhora a cobertura, mas não resolve tudo.","#EF9F27"),
            ("📦 Ruptura de picking","Há saldo em reserva, mas pouco no chão.","#378ADD"),
        ])
        st.markdown(f"**Venda inesperada.** O teto demonstrativo usa {mult_atipica:g}x a mediana ou o mínimo de {cap_min:g} peças.")
        st.markdown(f"**Abastecimento pela filial 011325.** Necessidade calculada para {dias_alvo:g} dias de cobertura.")
    render_ruptura(limites)

st.divider()
st.caption("MODO DEMONSTRAÇÃO: todos os SKUs, saldos, NFs, datas e resultados são fictícios. Nenhuma conexão externa é")
