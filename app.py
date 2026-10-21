"""Streamlit presentation; all business calculations live in src/analytics.py."""
import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.analytics import LABELS, read_data, report
from src.etl import ROOT, UNATTRIBUTED

st.set_page_config(page_title="Marketing Attribution Lab", layout="wide")
st.title("Marketing Attribution Lab")
st.warning("DEMONSTRAÇÃO — Dados e resultados sintéticos. Atribuição distribui crédito; não comprova causalidade.")
db = Path(os.environ.get("ATTRIBUTION_DB", ROOT / "data/processed/attribution.duckdb"))
if not db.exists():
    st.info("Prepare a base antes de abrir o painel. Execute, na raiz do projeto:")
    st.code("python -m src.sample_data\npython -m src.etl\npython -m streamlit run app.py", language="bash")
    st.stop()
try:
    data = read_data(db)
except Exception as exc:
    st.error(f"Não foi possível abrir a base: {exc}. Execute novamente python -m src.etl.")
    st.stop()
conversions = data["stg_conversions"]
if conversions.empty:
    st.info("A carga não possui conversões válidas. Verifique a qualidade dos dados abaixo.")
    st.dataframe(data["etl_summary"], hide_index=True)
    st.dataframe(data["etl_issues"], hide_index=True)
    st.stop()
start_default = conversions.converted_at.min().date()
end_default = conversions.converted_at.max().date()
st.sidebar.header("Filtros")
period = st.sidebar.date_input("Período de conversão (UTC)", value=(start_default, end_default))
if len(period) != 2:
    st.info("Selecione início e fim do período.")
    st.stop()
model = st.sidebar.selectbox("Modelo", list(LABELS), format_func=LABELS.get)
all_campaigns = sorted(set(data["stg_touchpoints"].campaign) | set(data["stg_campaign_spend"].campaign))
all_channels = sorted(set(data["stg_touchpoints"].channel) | set(data["stg_campaign_spend"].channel))
channels = st.sidebar.multiselect("Canais (vazio = todos)", all_channels)
campaigns = st.sidebar.multiselect("Campanhas (vazio = todas)", all_campaigns)
st.sidebar.button("Atualizar leitura")
try:
    result = report(data, *period, channels=channels, campaigns=campaigns)
except ValueError as exc:
    st.error(str(exc))
    st.stop()
metrics = result["metrics"].set_index("model")
current = metrics.loc[model]


def fmt(value, kind="number"):
    if value is None or pd.isna(value):
        return "N/A"
    rendered = f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if kind == "money":
        return f"R$ {rendered}"
    if kind == "rate":
        return fmt(float(value) * 100) + "%"
    if kind == "count":
        return str(int(value))
    return rendered


st.caption("Conversões por data da compra; leads = visitantes únicos com contato no período mais 7 dias anteriores. "
           "Investimento por data do gasto. Os modelos são calculados antes dos filtros.")
if current.missing_costs:
    st.warning("Investimento incompleto para: " + current.missing_costs +
               ". Investimento total, CAC e ROAS = N/A. Gasto conhecido: " + fmt(current.known_investment, "money"))
for items in [
    [("Leads únicos elegíveis", current.leads, "count"), ("Conversões atribuídas", current.conversions, "count"),
     ("Taxa de conversão", current.conversion_rate, "rate"), ("Receita atribuída", current.revenue, "money")],
    [("Investimento", current.investment, "money"), ("CAC", current.cac, "money"),
     ("ROAS", current.roas, "number"), ("Não atribuídas na seleção", current.unattributed, "count")],
]:
    for column, (label, value, kind) in zip(st.columns(4), items):
        column.metric(label, fmt(value, kind))
st.caption("Taxa = eventos de conversão atribuídos ÷ leads únicos. Com compras repetidas, ela pode ultrapassar 100%. "
           "O funil abaixo usa pessoas únicas nos dois estágios. ROAS é uma razão, não percentual.")
overview, comparison, audit, quality = st.tabs(["Visão geral", "Comparar modelos", "Conferência", "Qualidade da carga"])
with overview:
    st.subheader("Funil de dois estágios — pessoas únicas")
    funnel = go.Figure(go.Bar(x=[current.leads, current.converted_leads],
                             y=["Leads elegíveis", "Leads com conversão atribuída"],
                             orientation="h", marker_color=["#2563eb", "#15803d"]))
    funnel.update_layout(xaxis_title="Pessoas únicas", yaxis_autorange="reversed", height=260,
                         xaxis_rangemode="tozero", margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(funnel, use_container_width=True)
    campaign_table = result["campaigns"]
    selected = campaign_table.loc[campaign_table.model == model] if not campaign_table.empty else campaign_table
    chart_data = selected.loc[selected.campaign != UNATTRIBUTED] if not selected.empty else selected
    if not chart_data.empty:
        top = chart_data.nlargest(7, "revenue")
        money = top.melt(id_vars="campaign", value_vars=["revenue", "investment"], var_name="measure", value_name="amount")
        fig = px.bar(money, x="amount", y="campaign", color="measure", barmode="group", orientation="h",
                     color_discrete_map={"revenue": "#15803d", "investment": "#b91c1c"},
                     labels={"amount": "Valor (R$)", "campaign": "Campanha", "measure": "Medida"},
                     title="Receita e investimento — até 7 campanhas por receita")
        fig.update_xaxes(rangemode="tozero")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(selected, hide_index=True, use_container_width=True)
        st.caption("Tabela completa; pessoas podem aparecer em mais de uma campanha. Não some leads por campanha para obter o total. "
                   "Na tabela técnica, valores ausentes significam N/A; __unattributed__ significa não atribuída.")
    else:
        st.info("Nenhuma campanha encontrada para os filtros selecionados.")
with comparison:
    display_metrics = result["metrics"].drop(columns="missing_costs").copy()
    display_metrics["model"] = display_metrics.model.map(LABELS)
    st.dataframe(display_metrics, hide_index=True, use_container_width=True)
    if not result["campaigns"].empty:
        compared = result["campaigns"].loc[result["campaigns"].campaign != UNATTRIBUTED].copy()
        if not compared.empty:
            names = compared.groupby("campaign").conversions.sum().nlargest(7).index
            compared = compared.loc[compared.campaign.isin(names)]
            compared["model"] = compared.model.map(LABELS)
            fig = px.bar(compared, x="conversions", y="campaign", color="model", barmode="group", orientation="h",
                         color_discrete_map={"Primeiro contato": "#2563eb", "Último contato": "#7e22ce"},
                         labels={"conversions": "Conversões atribuídas", "campaign": "Campanha", "model": "Modelo"},
                         title="Primeiro × último contato — até 7 campanhas")
            fig.update_xaxes(rangemode="tozero", dtick=1 if compared.conversions.max() < 10 else None)
            st.plotly_chart(fig, use_container_width=True)
    st.info("O investimento e o universo de leads não mudam ao trocar o modelo. "
            "Mudam as campanhas/canais que recebem crédito pelas conversões e receita.")
with audit:
    st.subheader("Atribuição por conversão — modelo selecionado")
    details = result["detail"].loc[result["detail"].model == model]
    st.dataframe(details, hide_index=True, use_container_width=True)
    st.download_button("Baixar atribuição filtrada (CSV)", details.to_csv(index=False).encode("utf-8-sig"),
                       file_name="attribution_filtered.csv", mime="text/csv")
    st.subheader("Contatos que compõem o denominador de leads")
    st.dataframe(result["leads"], hide_index=True, use_container_width=True)
    st.subheader("Investimento filtrado — sem multiplicação por contatos ou modelos")
    st.dataframe(result["spend"], hide_index=True, use_container_width=True)
    with st.expander("Fontes aceitas completas (sem os filtros do painel)"):
        for name in ["stg_touchpoints", "stg_conversions", "stg_campaign_spend"]:
            st.write(name)
            st.dataframe(data[name], hide_index=True, use_container_width=True)
with quality:
    st.dataframe(data["etl_run"], hide_index=True)
    st.dataframe(data["etl_summary"], hide_index=True, use_container_width=True)
    st.caption("input_rows = accepted_rows + rejected_rows + duplicate_rows. warning_rows é subconjunto dos aceitos.")
    st.dataframe(data["etl_issues"], hide_index=True, use_container_width=True)
    st.download_button("Baixar ocorrências da carga", data["etl_issues"].to_csv(index=False).encode("utf-8-sig"),
                       file_name="etl_issues.csv", mime="text/csv")
with st.expander("Definições e limitações"):
    st.markdown("""
- Janela **[conversão − 7 dias, conversão)**; instantes normalizados para UTC.
- Empate de horário: menor `touchpoint_id` em ordem lexicográfica, nos dois modelos.
- Lead é uma **proxy**: visitante com contato válido. Não existe evento separado de cadastro.
- Taxa de conversão = conversões atribuídas / leads únicos elegíveis.
- CAC = investimento / conversões atribuídas; ROAS = receita atribuída / investimento.
- Denominador zero ou custo desconhecido: **N/A**. Gasto explicitamente zero é diferente de gasto ausente.
- Sem contato elegível: **não atribuída**. Com filtro de campanha/canal, a seleção não inclui a categoria não atribuída.
- Investimento no período e receita da coorte são comparações descritivas, não ROI causal.
- Somente dados sintéticos; sem APIs, autenticação ou uso de dados pessoais reais.
""")
