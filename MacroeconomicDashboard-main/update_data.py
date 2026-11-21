"""Refresh data.json for the Spanish macro dashboard.

Sources (all public, no keys):
  * INE        - Tempus3 JSON API (servicios.ine.es/wstempus)
  * Banco de España - BIE / boletín CSV files (bde.es/webbe/.../csv/<table>.csv)
  * Ministerio de Economía, Comercio y Empresa - BDSICE full download (BdsiceCSV.zip)

Nothing is estimated. Transformations are limited to year-on-year rates computed from
levels, rolling 4-quarter / 12-month sums and simple ratios, each one stated in the
series' metadata. Run:  python update_data.py data.json
"""
import csv, datetime as dt, io, json, sys, zipfile, time, re, unicodedata
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import requests

UA = {"User-Agent": "spain-macro-dashboard/1.0 (+github pages; public statistics)"}
INE = "https://servicios.ine.es/wstempus/js/ES/DATOS_SERIE/{c}?date=19950101:"
INE_LINK = "https://www.ine.es/consul/serie.do?s={c}"
BDE = "https://www.bde.es/webbe/es/estadisticas/compartido/datos/csv/{t}.csv"
BDE_LINK = "https://www.bde.es/webbe/es/estadisticas/temas/{t}"
MINECO_ZIP = "https://portal.mineco.gob.es/es-es/economiayempresa/EconomiaInformesMacro/Documents/BdsiceCSV.zip"
MINECO_LINK = "https://portal.mineco.gob.es/es-es/economiayempresa/EconomiaInformesMacro/Paginas/bdsice.aspx"
START = "2005-01-01"          # history kept in data.json
DAILY_START = "2021-01-01"    # daily market series

# ----------------------------------------------------------------------------------------
# Catalogue: id -> (source, code, label, unit, freq, block, extra)
#   source: ine | bde:<table> | mineco
#   extra:  dict with optional 'transform' ('yoy' = % change on a year earlier) and 'note'
# ----------------------------------------------------------------------------------------
CAT = [
    # Actividad - Contabilidad Nacional Trimestral (INE), datos CVEC, índices de volumen
    ("pib_qoq", "ine", "CNTR6653", "PIB · variación trimestral", "%", "Q", "actividad", {}),
    ("pib_yoy", "ine", "CNTR6654", "PIB · variación interanual", "%", "Q", "actividad", {}),
    ("dn_yoy", "ine", "CNTR7148", "Demanda nacional", "% a/a", "Q", "actividad", {}),
    ("ch_yoy", "ine", "CNTR7163", "Consumo de los hogares", "% a/a", "Q", "actividad", {}),
    ("cp_yoy", "ine", "CNTR7188", "Consumo de las AAPP", "% a/a", "Q", "actividad", {}),
    ("fbcf_yoy", "ine", "CNTR7213", "Formación bruta de capital fijo", "% a/a", "Q", "actividad", {}),
    ("x_yoy", "ine", "CNTR7267", "Exportaciones de bienes y servicios", "% a/a", "Q", "actividad", {}),
    ("m_yoy", "ine", "CNTR7287", "Importaciones de bienes y servicios", "% a/a", "Q", "actividad", {}),
    ("ap_dn", "ine", "CNTR7150", "Aportación de la demanda nacional", "pp", "Q", "actividad", {}),
    ("ap_de", "ine", "CNTR7264", "Aportación de la demanda externa", "pp", "Q", "actividad", {}),
    ("vab_agr", "ine", "CNTR6659", "VAB agricultura", "% a/a", "Q", "actividad", {}),
    ("vab_ind", "ine", "CNTR6664", "VAB industria", "% a/a", "Q", "actividad", {}),
    ("vab_con", "ine", "CNTR6674", "VAB construcción", "% a/a", "Q", "actividad", {}),
    ("vab_ser", "ine", "CNTR6679", "VAB servicios", "% a/a", "Q", "actividad", {}),
    ("pib_nom", "ine", "CNTR6548", "PIB a precios corrientes (sin ajustar)", "M€", "Q", "aux", {}),
    # Coyuntura mensual
    ("ipi_yoy", "ine", "IPI15374", "Producción industrial (CVEC)", "% a/a", "M", "coyuntura", {}),
    ("icm_yoy", "ine", "ICM3859", "Ventas del comercio minorista, precios constantes", "% a/a", "M", "coyuntura", {}),
    ("isa_ind", "mineco", "220000", "Indicador sintético de actividad · industria", "% a/a", "M", "coyuntura", {"transform": "yoy"}),
    ("isa_ser", "mineco", "240000", "Indicador sintético de actividad · servicios", "% a/a", "M", "coyuntura", {"transform": "yoy"}),
    ("isa_con", "mineco", "230090", "Indicador sintético de actividad · construcción", "% a/a", "M", "coyuntura", {"transform": "yoy"}),
    ("esi", "ecfin", "ES.ESI", "Indicador de sentimiento económico (CE)", "índice", "M", "coyuntura", {}),
    ("conf_cons", "ecfin", "ES.CONS", "Confianza del consumidor (CE)", "saldo", "M", "coyuntura", {}),
    ("cli", "oecd", "ESP.M.LI...AA...H", "Indicador adelantado compuesto OCDE (CLI)", "índice", "M", "coyuntura", {}),
    ("matric", "mineco", "271300", "Matriculaciones de turismos", "% a/a", "M", "coyuntura", {"transform": "yoy"}),
    ("elec", "mineco", "255010", "Consumo eléctrico corregido de temperatura y calendario", "% a/a", "M", "coyuntura", {}),
    ("turistas", "ine", "FREG286", "Entrada de turistas internacionales (Frontur)", "% a/a", "M", "coyuntura", {"transform": "yoy"}),
    ("gasto_tur", "ine", "FREG375", "Gasto de turistas internacionales (Egatur)", "% a/a", "M", "coyuntura", {"transform": "yoy"}),
    ("hipotecas", "ine", "HPT34618", "Hipotecas sobre viviendas (número)", "% a/a", "M", "coyuntura", {"transform": "yoy"}),
    # Vivienda (Ministerio de Vivienda y Agenda Urbana y Notariado, vía BDSICE; crédito, Banco de España)
    ("viv_visados", "mineco", "230410g", "Visados de obra nueva: viviendas (12 meses)", "miles", "M", "vivienda", {"transform": "sum12", "source": "Ministerio de Vivienda y Agenda Urbana (BDSICE)"}),
    ("viv_visados_yoy", "mineco", "230410g", "Visados de obra nueva: viviendas (12 meses)", "% a/a", "M", "vivienda", {"transform": "sum12yoy", "source": "Ministerio de Vivienda y Agenda Urbana (BDSICE)"}),
    ("viv_iniciadas", "mineco", "232020e", "Viviendas iniciadas (12 meses)", "miles", "M", "vivienda", {"transform": "sum12", "source": "Ministerio de Vivienda y Agenda Urbana (BDSICE)"}),
    ("viv_terminadas", "mineco", "232030e", "Viviendas terminadas (12 meses)", "miles", "M", "vivienda", {"transform": "sum12", "source": "Ministerio de Vivienda y Agenda Urbana (BDSICE)"}),
    ("viv_protegida", "mineco", "232640", "Vivienda protegida: calificaciones definitivas (12 meses)", "miles", "M", "vivienda", {"transform": "sum12", "source": "Ministerio de Vivienda y Agenda Urbana (BDSICE)"}),
    ("viv_compraventas", "mineco", "233700", "Compraventa de viviendas, notarios (12 meses)", "miles", "M", "vivienda", {"transform": "sum12", "source": "Consejo General del Notariado (BDSICE)"}),
    ("viv_compraventas_yoy", "mineco", "233700", "Compraventa de viviendas, notarios (12 meses)", "% a/a", "M", "vivienda", {"transform": "sum12yoy", "source": "Consejo General del Notariado (BDSICE)"}),
    ("viv_compr_reg", "ine", "ETDP1826", "Compraventa de viviendas inscritas, registros (12 meses)", "miles", "M", "vivienda", {"transform": "sum12", "source": "INE (Estadística de Transmisiones de Derechos de la Propiedad, Registros de la Propiedad)"}),
    ("viv_compr_reg_yoy", "ine", "ETDP1826", "Compraventa de viviendas inscritas, registros (12 meses)", "% a/a", "M", "vivienda", {"transform": "sum12yoy", "source": "INE (Estadística de Transmisiones de Derechos de la Propiedad, Registros de la Propiedad)"}),
    ("viv_tasado", "mineco", "449990", "Valor tasado de la vivienda libre", "€/m²", "Q", "vivienda", {"source": "Ministerio de Vivienda y Agenda Urbana (BDSICE)"}),
    ("viv_tasado_yoy", "mineco", "449990", "Valor tasado de la vivienda libre", "% a/a", "Q", "vivienda", {"transform": "yoy", "source": "Ministerio de Vivienda y Agenda Urbana (BDSICE)"}),
    ("viv_coste", "mineco", "470111", "Coste de construcción residencial", "% a/a", "M", "vivienda", {"transform": "yoy", "source": "Ministerio de Vivienda y Agenda Urbana (BDSICE)"}),
    ("viv_credito", "bde:be0413", "D_MEE62100", "Crédito a hogares para adquisición de vivienda (saldo)", "% a/a", "Q", "vivienda", {"transform": "yoy", "note": "Variación del saldo vivo; no corrige saneamientos ni reclasificaciones."}),
    # Crédito y nuevas operaciones: series originales del Banco de España
    ('aux_cr_cons', 'bde:be0413', 'D_MEE62300', 'Crédito a hogares para consumo', 'miles €', 'Q', 'aux', {}),
    ('aux_cr_prod', 'bde:be0413', 'D_MEE61000', 'Crédito para actividades productivas', 'miles €', 'Q', 'aux', {}),
    ('aux_cr_viv', 'bde:be0413', 'D_MEE62100', 'Crédito a hogares para vivienda', 'miles €', 'Q', 'aux', {}),
    ('aux_du_cons', 'bde:be0413', 'D_MEADU226', 'Dudosos: consumo', 'miles €', 'Q', 'aux', {}),
    ('aux_du_prod', 'bde:be0413', 'D_MEADU100', 'Dudosos: actividades productivas', 'miles €', 'Q', 'aux', {}),
    ('aux_du_viv', 'bde:be0413', 'D_MEADU210', 'Dudosos: vivienda', 'miles €', 'Q', 'aux', {}),
    ('cred_cons_yoy', 'bde:be0413', 'D_MEE62300', 'Crédito a hogares para consumo (saldo)', '% a/a', 'Q', 'credito', {'transform': 'yoy', 'note': 'Variación del saldo vivo; no corrige saneamientos ni reclasificaciones.'}),
    ('cred_prod_yoy', 'bde:be0413', 'D_MEE61000', 'Crédito para actividades productivas (saldo)', '% a/a', 'Q', 'credito', {'transform': 'yoy', 'note': 'Variación del saldo vivo; no corrige saneamientos ni reclasificaciones.'}),
    ('nop_cons', 'bde:be1911', 'DN_1TI2TIE99', 'Nuevas operaciones de crédito al consumo (12 meses)', 'mm€', 'M', 'credito', {'transform': 'sum12'}),
    ('nop_snf', 'bde:be1911', 'DN_1TI2TIE105', 'Nuevas operaciones de crédito a empresas (12 meses)', 'mm€', 'M', 'credito', {'transform': 'sum12'}),
    ('nop_viv', 'bde:be1911', 'DN_1TI2TIE96', 'Nuevas operaciones de crédito a la vivienda (12 meses)', 'mm€', 'M', 'credito', {'transform': 'sum12'}),
    ('nop_cons_yoy', 'bde:be1911', 'DN_1TI2TIE99', 'Nuevas operaciones de crédito al consumo (12 meses)', '% a/a', 'M', 'credito', {'transform': 'sum12yoy'}),
    ('nop_snf_yoy', 'bde:be1911', 'DN_1TI2TIE105', 'Nuevas operaciones de crédito a empresas (12 meses)', '% a/a', 'M', 'credito', {'transform': 'sum12yoy'}),
    ('nop_viv_yoy', 'bde:be1911', 'DN_1TI2TIE96', 'Nuevas operaciones de crédito a la vivienda (12 meses)', '% a/a', 'M', 'credito', {'transform': 'sum12yoy'}),
    # Saldos, dudosidad y tipos: series originales del Banco de España
    ('cred_dudosidad', 'bde:be0403', 'D_BEEA2020', 'Ratio de dudosidad del crédito a otros sectores residentes', '%', 'M', 'credito', {}),
    ('cred_saldo', 'bde:be0403', 'D_BEEA1000', 'Crédito a otros sectores residentes (saldo)', 'mm€', 'M', 'credito', {'scale': 1e-06}),
    ('dep_plazo_yoy', 'bde:be0405', 'D_BEEP1199', 'Depósitos a plazo y con preaviso de otros sectores residentes', '% a/a', 'M', 'credito', {'transform': 'yoy'}),
    ('dep_saldo', 'bde:be0405', 'D_BEEP0000N', 'Depósitos de otros sectores residentes (saldo)', 'mm€', 'M', 'credito', {'scale': 1e-06}),
    ('dep_vista_yoy', 'bde:be0405', 'D_BEEP1188', 'Depósitos a la vista de otros sectores residentes', '% a/a', 'M', 'credito', {'transform': 'yoy'}),
    ('tipo_cons', 'bde:be1903', 'DN_1TI2T0138', 'Tipo nuevas operaciones de crédito al consumo (TEDR)', '%', 'M', 'credito', {}),
    ('tipo_dep_hog', 'bde:be1907', 'DN_1TI2T0024', 'Tipo nuevos depósitos a plazo de hogares (TEDR)', '%', 'M', 'credito', {}),
    ('tipo_dep_snf', 'bde:be1907', 'DN_1TI2T0029', 'Tipo nuevos depósitos a plazo de empresas (TEDR)', '%', 'M', 'credito', {}),
    ('tipo_dep_vista', 'bde:be1907', 'DN_1TI1T0010', 'Tipo de los depósitos a la vista de hogares (TEDR)', '%', 'M', 'credito', {}),
    ('tipo_snf', 'bde:be1903', 'DN_1TI2T0144', 'Tipo nuevas operaciones de crédito a empresas (TEDR)', '%', 'M', 'credito', {}),
    ('tipo_viv_saldo', 'bde:be1908', 'DN_1TI1T0050', 'Tipo del saldo vivo de crédito a la vivienda (TEDR)', '%', 'M', 'credito', {}),
    # Costes laborales ETCL: valores originales del INE, variación interanual
    ('sal_ord', 'ine', 'ETCL66', 'Coste salarial ordinario por trabajador y mes (ETCL)', '% a/a', 'Q', 'precios', {'transform': 'yoy'}),
    ('sal_etcl', 'ine', 'ETCL67', 'Coste salarial total por trabajador y mes (ETCL)', '% a/a', 'Q', 'precios', {'transform': 'yoy'}),
    ('clab_etcl', 'ine', 'ETCL68', 'Coste laboral total por trabajador y mes (ETCL)', '% a/a', 'Q', 'precios', {'transform': 'yoy'}),
    # Convenios colectivos: avance mensual del Ministerio de Trabajo
    ("sal_conv", "cct", "CCT-2.1", "Incremento salarial pactado en convenios (acumulado en el año)", "%", "M", "precios", {"note": "Avance del Ministerio de Trabajo; años anteriores a 2025 conservados desde BDSICE. Cifras oficiales a dos decimales."}),
    # Mercado laboral
    ("paro_epa", "ine", "EPA423474", "Tasa de paro (EPA)", "%", "Q", "empleo", {}),
    ("ocup_epa", "ine", "EPA387796", "Ocupados (EPA)", "miles", "Q", "empleo", {}),
    ("ocup_epa_yoy", "ine", "EPA387796", "Ocupados (EPA)", "% a/a", "Q", "empleo", {"transform": "yoy"}),
    # Fuentes primarias: Seguridad Social (tablas PX de afiliados medios) y SEPE / Ministerio de Trabajo (paro registrado)
    ("afil_sa", "ss", "sa", "Afiliados a la Seguridad Social (desestacionalizados)", "miles", "M", "empleo", {"scale": 0.001}),
    ("afil_yoy", "ss", "nsa", "Afiliados a la Seguridad Social (media mensual)", "% a/a", "M", "empleo", {"transform": "yoy"}),
    ("afil_q_yoy", "ss", "nsa", "Afiliados a la Seguridad Social (media trimestral)", "% a/a", "Q", "empleo", {"transform": "qavg_yoy"}),
    ("paro_reg", "sepe", "total", "Paro registrado", "miles", "M", "empleo", {"scale": 0.001}),
    ("paro_reg_yoy", "sepe", "total", "Paro registrado", "% a/a", "M", "empleo", {"transform": "yoy"}),
    # Precios
    ("ipc", "ine", "IPC290750", "IPC general", "% a/a", "M", "precios", {}),
    ("ipc_sub", "ine", "IPC292510", "IPC subyacente", "% a/a", "M", "precios", {}),
    ("ipc_ene", "ine", "IPC292458", "IPC productos energéticos", "% a/a", "M", "precios", {}),
    ("ipc_ali", "ine", "IPC292534", "IPC alimentos, bebidas y tabaco", "% a/a", "M", "precios", {}),
    ("ipc_ser", "ine", "IPC292494", "IPC servicios", "% a/a", "M", "precios", {}),
    ("ipca", "ine", "IPCA8090", "IPCA (armonizado)", "% a/a", "M", "precios", {}),
    ("dif_uem", "mineco", "409096c", "Diferencial de inflación IPCA España − UEM", "pp", "M", "precios", {}),
    ("ipv", "ine", "IPV1376", "Precio de la vivienda (IPV)", "% a/a", "Q", "precios", {}),
    # Sector público
    ("deuda", "bde:be1102", "DTNPDE2010_P0000P_PS_APU", "Deuda de las AAPP (PDE)", "% PIB", "Q", "publico", {}),
    ("deficit_q", "mineco", "770t000000", "Capacidad/necesidad de financiación AAPP", "M€", "Q", "aux", {}),
    ("deficit_m", "mineco", "770m1000001p", "Saldo AC+CCAA+SS acumulado en el año", "% PIB", "M", "publico", {}),
    # Sector exterior (Banco de España, balanza de pagos)
    ("cc_m", "bde:be1701", "DEEM.N.ES.W1.S1.S1.T.B.CA._Z._Z._Z.EUR._T._X.N.ALL", "Saldo por cuenta corriente", "M€", "M", "aux", {}),
    ("bys_m", "bde:be1701", "DEEM.N.ES.W1.S1.S1.T.B.GS._Z._Z._Z.EUR._T._X.N.ALL", "Saldo de bienes y servicios", "M€", "M", "aux", {}),
    ("tur_ing_m", "bde:be1703", "DEEM.N.ES.W1.S1.S1.T.C.SD._Z._Z._Z.EUR._T._X.N.ALL", "Ingresos por turismo", "M€", "M", "aux", {}),
    # Financiero
    ("bono_es", "mineco", "867320q", "Rentabilidad bono 10 años · España", "%", "D", "financiero", {}),
    ("bono_de", "mineco", "867321q", "Rentabilidad bono 10 años · Alemania", "%", "D", "financiero", {}),
    ("bce_dfr", "ecb", "FM.D.U2.EUR.4F.KR.DFR.LEV", "BCE · facilidad de depósito", "%", "D", "financiero", {}),
    ("euribor12", "bde:be1901", "D_1NBAF472", "Euríbor a 12 meses", "%", "M", "financiero", {}),
    ("euribor12_daily", "bde:ti_1_7", "D_DNBAF172", "Euríbor a 12 meses · diario", "%", "D", "financiero", {"note": "Observación diaria publicada por el Banco de España; puede estar disponible con retraso."}),
    ("tipo_viv", "bde:be1904", "DN_1TI2T0002", "Tipo nuevas hipotecas (TEDR)", "%", "M", "financiero", {}),
    ("credito_osr", "bde:be0403", "D_BEEA1000", "Crédito a otros sectores residentes (saldo)", "% a/a", "M", "financiero", {"transform": "yoy", "note": "Variación del saldo vivo; no corrige saneamientos ni reclasificaciones."}),
    ("depositos_osr", "bde:be0405", "D_BEEP0000N", "Depósitos de otros sectores residentes (saldo)", "% a/a", "M", "financiero", {"transform": "yoy"}),
]

SOURCE_NAME = {"ine": "INE", "bde": "Banco de España", "mineco": "Ministerio de Economía (BDSICE)"}
MESES = {m: i + 1 for i, m in enumerate("ENE FEB MAR ABR MAY JUN JUL AGO SEP OCT NOV DIC".split())}


def get(url, tries=3, **kw):
    for k in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=180, **kw); r.raise_for_status(); return r
        except Exception:
            if k == tries - 1: raise
            time.sleep(3 * (k + 1))


# ---------------------------------------------------------------- Fuentes originales: BCE y Comisión Europea
ECB_DATA = "https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV?format=csvdata&startPeriod=2021-01-01"
ECB_INFO = "https://data.ecb.europa.eu/data/datasets/FM/FM.D.U2.EUR.4F.KR.DFR.LEV"
ECFIN_PAGE = "https://economy-finance.ec.europa.eu/economic-forecast-and-surveys/business-and-consumer-surveys/download-business-and-consumer-survey-data/time-series_en"
_ECFIN = None

class _MainIndicatorsLink(HTMLParser):
    def __init__(self):
        super().__init__(); self.links = []
    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href", "")
            if "main_indicators_sa_nace2.zip" in href.lower(): self.links.append(href)

def ecb_series(code):
    content = get(ECB_DATA).content.decode("utf-8-sig")
    rows = csv.DictReader(io.StringIO(content)); out = []
    if not {"KEY", "TIME_PERIOD", "OBS_VALUE"}.issubset(rows.fieldnames or []):
        raise ValueError("BCE: formato CSV inesperado")
    for row in rows:
        if row.get("KEY") != code or not row.get("OBS_VALUE"): continue
        try:
            d = dt.date.fromisoformat(row["TIME_PERIOD"]).isoformat(); v = float(row["OBS_VALUE"])
        except (ValueError, TypeError): continue
        if not -10 <= v <= 30: raise ValueError("BCE: tipo fuera de límites plausibles")
        out.append([d, v, 0])
    out.sort()
    if not out: raise ValueError("BCE: serie diaria vacía")
    return "Facilidad de depósito del BCE, tipo diario", out

def ecfin_series(code):
    global _ECFIN
    if _ECFIN is None:
        import openpyxl
        parser = _MainIndicatorsLink(); parser.feed(get(ECFIN_PAGE).content.decode("utf-8", "replace"))
        urls = {urljoin(ECFIN_PAGE, href) for href in parser.links}
        urls = {u for u in urls if urlparse(u).scheme == "https" and urlparse(u).hostname in {"ec.europa.eu", "economy-finance.ec.europa.eu"}}
        if len(urls) != 1: raise ValueError("Comisión Europea: enlace al ZIP principal no identificado de forma única")
        archive = zipfile.ZipFile(io.BytesIO(get(urls.pop()).content))
        names = [n for n in archive.namelist() if n.lower().endswith(".xlsx") and not n.startswith("__MACOSX/")]
        if len(names) != 1: raise ValueError("Comisión Europea: libro de indicadores no identificado de forma única")
        book = openpyxl.load_workbook(io.BytesIO(archive.read(names[0])), read_only=True, data_only=True)
        try:
            sheet = book["MONTHLY"]; rows = sheet.iter_rows(values_only=True); header = next(rows)
            indices = {c: header.index(c) for c in ("ES.ESI", "ES.CONS")}
            parsed = {c: [] for c in indices}
            for row in rows:
                if not isinstance(row[0], (dt.date, dt.datetime)): continue
                date = row[0].strftime("%Y-%m-01")
                for key, ix in indices.items():
                    value = row[ix] if ix < len(row) else None
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        parsed[key].append([date, float(value), 0])
            if not all(parsed.values()): raise ValueError("Comisión Europea: indicador español sin observaciones")
            _ECFIN = parsed
        finally: book.close()
    return {"ES.ESI": "Economic Sentiment Indicator, España, ajustado estacionalmente", "ES.CONS": "Consumer confidence indicator, España, ajustado estacionalmente"}[code], _ECFIN[code]

OECD_CLI_URL = "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI,4.1/ESP.M.LI...AA...H?startPeriod=2005-01&dimensionAtObservation=AllDimensions&format=csvfilewithlabels"
OECD_CLI_INFO = "https://www.oecd.org/en/data/datasets/oecd-composite-leading-indicators-clis.html"

def oecd_cli_series(code):
    if code != "ESP.M.LI...AA...H": raise ValueError("OCDE: código no reconocido")
    rows = csv.DictReader(io.StringIO(get(OECD_CLI_URL).content.decode("utf-8-sig")))
    dims = {"REF_AREA":"ESP", "FREQ":"M", "MEASURE":"LI", "UNIT_MEASURE":"IX", "ACTIVITY":"_Z", "ADJUSTMENT":"AA", "TRANSFORMATION":"IX", "TIME_HORIZ":"_Z", "METHODOLOGY":"H"}
    if not set(dims).union({"TIME_PERIOD", "OBS_VALUE"}).issubset(rows.fieldnames or []):
        raise ValueError("OCDE: formato CSV inesperado")
    obs = {}
    for row in rows:
        if any(row.get(k) != v for k, v in dims.items()) or not row.get("OBS_VALUE"): continue
        period = row["TIME_PERIOD"]
        try:
            date = dt.date.fromisoformat(period+"-01").isoformat()
            value = float(row["OBS_VALUE"])
        except (ValueError, TypeError): continue
        if not __import__("math").isfinite(value): raise ValueError("OCDE: valor no finito")
        if date in obs: raise ValueError("OCDE: más de una observación para un mes")
        obs[date] = value
    if not obs: raise ValueError("OCDE: indicador español mensual sin observaciones")
    return "Indicador adelantado compuesto (CLI), España, ajustado por amplitud", [[d,v,0] for d,v in sorted(obs.items())]

CCT_SEARCH = "https://cpage.mpr.gob.es/publicaciones/buscar/?q=estadistica+de+convenios+colectivos+de+trabajo+avance+mensual"
CCT_SOURCE = "Ministerio de Trabajo y Economía Social (histórico BDSICE)"

class _Links(HTMLParser):
    def __init__(self): super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        if tag.lower()=="a":
            href=dict(attrs).get("href")
            if href: self.links.append(href)

def cct_series(code):
    if code!="CCT-2.1": raise ValueError("Convenios: cuadro no reconocido")
    import xlrd
    parser=_Links(); parser.feed(get(CCT_SEARCH).content.decode("utf-8", "replace")); candidates={}
    pattern=re.compile(r"/publicacion/estadistica-de-convenios-colectivos-de-trabajo-avance-mensual-(\d{4})-[0-9a-z]+-(\d{4})/?$", re.I)
    for href in parser.links:
        url=urljoin(CCT_SEARCH, href); parsed=urlparse(url); match=pattern.search(parsed.path)
        if parsed.scheme=="https" and parsed.hostname=="cpage.mpr.gob.es" and match:
            year,issue=map(int,match.groups())
            if 2015 <= year <= dt.date.today().year: candidates[(year,issue)]=url
    if not candidates: raise ValueError("Convenios: no se ha localizado una publicación mensual oficial")
    publication=candidates[max(candidates)]; page=_Links();page.feed(get(publication).content.decode("utf-8", "replace"))
    downloads={urljoin(publication,h) for h in page.links if "visordocumentosicopo.aspx" in h.lower()}
    downloads={u for u in downloads if urlparse(u).scheme=="https" and urlparse(u).hostname=="servicios.mpr.es"}
    if len(downloads)!=1: raise ValueError("Convenios: no se ha identificado un único XLS oficial")
    book=xlrd.open_workbook(file_contents=get(next(iter(downloads))).content)
    try:
        sheet=book.sheet_by_name("CCT-2.1")
        norm=lambda value: unicodedata.normalize("NFKD",str(value)).encode("ascii","ignore").decode("ascii").upper()
        header=" ".join(norm(sheet.cell_value(0,c)) for c in range(sheet.ncols))
        months_full={"ENERO":1,"FEBRERO":2,"MARZO":3,"ABRIL":4,"MAYO":5,"JUNIO":6,"JULIO":7,"AGOSTO":8,"SEPTIEMBRE":9,"OCTUBRE":10,"NOVIEMBRE":11,"DICIEMBRE":12}
        match=re.search(r"REGISTRADOS HASTA\s+("+"|".join(months_full)+r")\s+(20\d{2})",header)
        if not match: raise ValueError("Convenios: fecha de registro no identificada en el cuadro CCT-2.1")
        target_year,target_month=int(match.group(2)),months_full[match.group(1)]
        if target_year!=max(candidates)[0] or dt.date(target_year,target_month,1)>dt.date.today(): raise ValueError("Convenios: fecha del libro no coincide con la publicación")
        short={k[:3]:v for k,v in months_full.items()}; year=None; obs={}
        for r in range(sheet.nrows):
            label=norm(sheet.cell_value(r,0)).strip()
            year_match=re.match(r"^(20\d{2})\s*\(4\)", label)
            if year_match: year=int(year_match.group(1)); continue
            if year is None: continue
            month=short.get(label[:3]); value=sheet.cell_value(r,4)
            if month and isinstance(value,(int,float)) and not isinstance(value,bool):
                date=f"{year:04d}-{month:02d}-01"
                if date in obs: raise ValueError("Convenios: mes duplicado en CCT-2.1")
                if not -20<=value<=50: raise ValueError("Convenios: variación salarial fuera de rango")
                obs[date]=float(value)
        expected=f"{target_year:04d}-{target_month:02d}-01"
        if not obs or max(obs)!=expected: raise ValueError("Convenios: la última observación no coincide con el periodo de publicación")
        return "Variación salarial pactada, avance mensual, total de convenios", [[d,v,1] for d,v in sorted(obs.items())], publication
    finally: book.release_resources()

def check_primary_transition(previous_series, observations, new_source, sid):
    """Avoid silently changing the meaning of a series or downgrading its latest date."""
    if not previous_series or not previous_series.get("data"): return
    previous = {x[0]: x[1] for x in previous_series["data"] if len(x)>1 and isinstance(x[1], (int,float))}
    new = {x[0]: x[1] for x in observations if len(x)>1 and isinstance(x[1], (int,float))}
    if not new: raise ValueError(f"{sid}: fuente original sin datos numéricos")
    if max(new) < max(previous): raise ValueError(f"{sid}: la fuente original está menos actualizada que los datos conservados")
    if previous_series.get("source") == new_source: return
    shared = sorted(set(previous) & set(new))[-120:]
    if len(shared) < 12: raise ValueError(f"{sid}: solapamiento insuficiente para verificar la serie original")
    match = sum(abs(previous[d]-new[d]) <= 0.051 for d in shared)
    if match < 0.95*len(shared): raise ValueError(f"{sid}: discrepancias con la serie anterior; revisar metodología")

# ---------------------------------------------------------------- INE
def ine_series(code):
    d = get(INE.format(c=code)).json()
    out = []
    for x in d.get("Data", []):
        if x.get("Valor") is None or x.get("Secreto"): continue
        y, p = x["Anyo"], x["FK_Periodo"]
        if 1 <= p <= 12: date = f"{y}-{p:02d}-01"
        elif 19 <= p <= 22: date = f"{y}-{(p - 19) * 3 + 1:02d}-01"
        else:  # fall back to the timestamp (Madrid midnight, so add 12h before taking the date)
            date = dt.datetime.utcfromtimestamp(x["Fecha"] / 1000 + 43200).strftime("%Y-%m-01")
        out.append([date, float(x["Valor"]), 1 if x.get("FK_TipoDato") == 3 else 0])
    out.sort()
    return d.get("Nombre", "").strip(), out


# ---------------------------------------------------------------- Banco de España
_BDE = {}
def bde_table(t):
    if t not in _BDE:
        txt = get(BDE.format(t=t)).content.decode("latin1")
        _BDE[t] = list(csv.reader(io.StringIO(txt)))
    return _BDE[t]

def bde_date(s):
    p = s.strip().split()
    if len(p) == 2 and p[0] in MESES: return f"{int(p[1])}-{MESES[p[0]]:02d}-01"
    if len(p) == 3 and p[1] in MESES: return f"{int(p[2])}-{MESES[p[1]]:02d}-{int(p[0]):02d}"
    return None

def bde_series(t, code):
    rows = bde_table(t); codes = [c.strip() for c in rows[0][1:]]
    i = codes.index(code) + 1
    desc = next((r[i] for r in rows[:8] if r and r[0].startswith("DESCRIPCI")), "")
    out = []
    for r in rows:
        if not r: continue
        d = bde_date(r[0])
        if not d or i >= len(r) or r[i] in ("", "_"): continue
        try: out.append([d, float(r[i]), 0])
        except ValueError: pass
    return desc, out


# ---------------------------------------------------------------- Ministerio (BDSICE)
_ZIP = None
# ---------------------------------------------------------------- Seguridad Social (PX-Web, afiliados medios)
SS_BASE = "https://w6.seg-social.es/PXWeb/Resources/PX/Databases/Afiliados%20en%20alta%20laboral/Afiliados%20Medios/"
SS_LINK = "https://w6.seg-social.es/PXWeb/pxweb/es/Afiliados%20en%20alta%20laboral/Afiliados%20en%20alta%20laboral__Afiliados%20Medios/"
SS_TABLES = {  # older table first; the newer one overrides from its first period onwards
    "nsa": ["10m09. Afiliados medios por dependencia laboral y sector de actividad CNAE 09 (ene-08 a dic-25).px",
            "10m25. Afiliados medios por dependencia laboral y sector de actividad CNAE 25 (desde ene-21).px"],
    "sa":  ["10mb09. Afiliados medios DESESTACIONALIZADOS por dependencia laboral y sector de actividad CNAE 09 (ene-08 a dic-25).px",
            "10mb25. Afiliados medios DESESTACIONALIZADOS por dependencia laboral y sector de actividad CNAE 25 (desde ene-21).px"]}
def _px_total(name):
    import re, urllib.parse
    txt = get(SS_BASE + urllib.parse.quote(name)).content.decode("latin1")
    blk = lambda k: re.search(r'%s=(.*?);' % k, txt, re.S).group(1)
    q = lambda s: re.findall(r'"([^"]*)"', s)
    dims = q(blk("STUB")) + q(blk("HEADING"))
    vals = {d: q(re.search(r'VALUES\("%s"\)=(.*?);' % re.escape(d), txt, re.S).group(1)) for d in dims}
    data = blk("DATA").split(); size = 1
    for d in dims[1:]: size *= len(vals[d])
    if len(data) != size * len(vals[dims[0]]): raise ValueError("PX: tamaño inesperado en " + name)
    out = {}
    for i, p in enumerate(vals[dims[0]]):   # first cell of each period block = Total sector, Total dependencia
        v = data[i * size]
        if v.replace(".", "").replace("-", "").isdigit(): out[f"{p[:4]}-{p[4:6]}-01"] = float(v)
    return out
def ss_series(kind):
    merged = {}
    for name in SS_TABLES[kind]:
        part = _px_total(name)
        if merged and part: merged = {d: v for d, v in merged.items() if d < min(part)}
        merged.update(part)
    title = "Afiliados medios, total sistema" + (" (desestacionalizados)" if kind == "sa" else "")
    return title, [[d, v, 0] for d, v in sorted(merged.items())]

# ---------------------------------------------------------------- SEPE / Ministerio de Trabajo (paro registrado)
SEPE_BASE = "https://www.sepe.es/SiteSepe/contenidos/que_es_el_sepe/estadisticas/datos_avance/xls/empleo/"
SEPE_LINK = "https://www.sepe.es/HomeSepe/que-es-el-sepe/estadisticas/datos-avance/paro.html"
def sepe_series(kind="total"):
    import pandas as pd   # the SEPE .xls files are read with the calamine engine (xlrd rejects the recent one)
    out = {}
    for fname in ("evolparoseries2001.xls", "evolparoseries.xls"):   # 2001-2016 and last 10 years
        sh = pd.read_excel(io.BytesIO(get(SEPE_BASE + fname).content), sheet_name=0, header=None, engine="calamine")
        for row in sh.itertuples(index=False):   # "Sectores": yyyymm, month name, TOTAL, ...
            row = list(row)
            for i, c in enumerate(row[:-2]):
                try: c = float(c); tot = float(row[i + 2])
                except (TypeError, ValueError): continue
                if 200001 <= c <= 210012 and c == int(c) and tot == tot:
                    p = str(int(c)); out[f"{p[:4]}-{p[4:6]}-01"] = tot; break
    return "Paro registrado, total (SEPE)", [[d, v, 0] for d, v in sorted(out.items())]

def mineco_series(code):
    global _ZIP
    if _ZIP is None:
        import os   # BDSICE_ZIP_FILE lets a local copy of BdsiceCSV.zip stand in for the download
        local = os.environ.get("BDSICE_ZIP_FILE")
        _ZIP = zipfile.ZipFile(local if local else io.BytesIO(get(MINECO_ZIP).content))
    raw = _ZIP.read(code + ".csv").decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(raw), delimiter=";"))
    h = rows[0]; iobs = h.index("Observaciones"); title = rows[1][h.index("Título")]
    per = h[:iobs]; out = []
    for r in rows[1:]:
        if len(r) <= iobs or r[iobs].strip() == "": continue
        y = int(r[0])
        if len(per) == 1: d = f"{y}-01-01"
        elif per[1].startswith("Trimestre"): d = f"{y}-{(int(r[1]) - 1) * 3 + 1:02d}-01"
        elif len(per) == 2: d = f"{y}-{int(r[1]):02d}-01"
        else: d = f"{y}-{int(r[1]):02d}-{int(r[2]):02d}"
        try: out.append([d, float(r[iobs].replace(",", ".")), 0])
        except ValueError: continue   # 'ND' and other non-numeric markers
    out.sort()
    return title, out


# ---------------------------------------------------------------- transforms
def yoy(obs, freq):
    lag = {"M": 12, "Q": 4}[freq]; out = []
    idx = {o[0]: o[1] for o in obs}
    for d, v, f in obs:
        y, m = int(d[:4]), d[5:7]
        prev = idx.get(f"{y - 1}-{m}-01")
        if prev not in (None, 0): out.append([d, round(100 * (v / prev - 1), 2), f])
    return out

def _months(d): return int(d[:4]) * 12 + int(d[5:7])
def rolling_sum(obs, n):
    out = []
    for i in range(n - 1, len(obs)):
        w = obs[i - n + 1:i + 1]
        step = (_months(w[-1][0]) - _months(w[0][0])) / (n - 1) if n > 1 else 1
        if step not in (1, 3): continue   # window has gaps: skip
        out.append([obs[i][0], sum(x[1] for x in w), max(x[2] for x in w)])
    return out

def ratio_pct(num, den):
    di = {d: v for d, v, _ in den}; out = []
    for d, v, f in num:
        if d in di and di[d]: out.append([d, round(100 * v / di[d], 2), f])
    return out

def q_of(d): return f"{d[:4]}-{(int(d[5:7]) - 1) // 3 * 3 + 1:02d}-01"


END_OF_PERIOD = {"deficit_m", "cc_pib", "bys_pib", "tur_ing_12m", "cred_dudosidad", "cred_saldo", "dep_saldo", "sal_conv"}   # 12-month sums and year-to-date balances
def agg_monthly(sid, obs, level, tr):
    """Annual and quarterly figures for monthly series (complete periods only):
       growth  - % change of the period average of the published level (rates computed from levels)
       end     - value in the last month of the period (12-month sums, year-to-date balances)
       mean    - average of the months (published rates, indices, balances and levels)"""
    how = "end" if (tr in ("sum12", "sum12yoy") or sid in END_OF_PERIOD) else ("growth" if level else "mean")
    def group(rows, key, n):
        g = {}
        for d, v, *_ in rows: g.setdefault(key(d), []).append((d, v))
        return {k: v for k, v in g.items() if len(v) == n}
    yk, qk = (lambda d: d[:4]), (lambda d: q_of(d))
    if how == "end":
        A = {k: v[-1][1] for k, v in group(obs, yk, 12).items()}; Q = {k: v[-1][1] for k, v in group(obs, qk, 3).items()}
    elif how == "mean":
        A = {k: sum(x for _, x in v) / 12 for k, v in group(obs, yk, 12).items()}; Q = {k: sum(x for _, x in v) / 3 for k, v in group(obs, qk, 3).items()}
    else:
        la = {k: sum(x for _, x in v) / 12 for k, v in group(level, yk, 12).items()}; lq = {k: sum(x for _, x in v) / 3 for k, v in group(level, qk, 3).items()}
        A = {k: 100 * (v / la[str(int(k) - 1)] - 1) for k, v in la.items() if str(int(k) - 1) in la}
        Q = {k: 100 * (v / lq[str(int(k[:4]) - 1) + k[4:]] - 1) for k, v in lq.items() if str(int(k[:4]) - 1) + k[4:] in lq}
    return {"annual": {k: round(v, 3) for k, v in A.items() if k >= "2015"},
            "quarterly": {k: round(v, 3) for k, v in Q.items() if k >= "2015"}, "annual_how": how}

def main(path):
    series, errors = {}, []
    try: previous = json.load(open(path, encoding="utf-8")).get("series", {})
    except Exception: previous = {}
    # Preserve series from the newer dashboard that this older catalogue does not yet refresh.
    import copy
    series = copy.deepcopy(previous)
    catalogue_ids = {entry[0] for entry in CAT}
    derived_ids = {"deficit_4q", "cc_pib", "bys_pib", "tur_ing_12m", "prima", "dud_cons", "dud_prod", "dud_viv"}
    preserved = sorted(set(previous) - catalogue_ids - derived_ids)
    if preserved: errors.append("Series conservadas sin actualización en este catálogo: " + ", ".join(preserved))
    for sid, src, code, label, unit, freq, block, extra in CAT:
        try:
            if src == "cct": name, obs, link = cct_series(code); sname = CCT_SOURCE
            elif src == "oecd": name, obs = oecd_cli_series(code); link = OECD_CLI_INFO; sname = "OCDE"
            elif src == "ecb": name, obs = ecb_series(code); link = ECB_INFO; sname = "Banco Central Europeo"
            elif src == "ecfin": name, obs = ecfin_series(code); link = ECFIN_PAGE; sname = "Comisión Europea (DG ECFIN)"
            elif src == "ine": name, obs = ine_series(code); link = INE_LINK.format(c=code); sname = "INE"
            elif src.startswith("bde:"):
                t = src.split(":")[1]; name, obs = bde_series(t, code); link = BDE.format(t=t); sname = "Banco de España"
            elif src == "ss": name, obs = ss_series(code); link = SS_LINK; sname = "Seguridad Social"
            elif src == "sepe": name, obs = sepe_series(code); link = SEPE_LINK; sname = "SEPE (Ministerio de Trabajo)"
            else: name, obs = mineco_series(code); link = MINECO_LINK; sname = "Ministerio de Economía (BDSICE)"
            tr = extra.get("transform")
            if extra.get("scale"): obs = [[d, v * extra["scale"], f] for d, v, f in obs]
            level = obs if (tr == "yoy" and freq == "M") else None   # kept to aggregate growth rates by year and quarter
            if tr == "yoy": obs = yoy(obs, freq)
            elif tr == "sum12": obs = [[d, round(v / 1000, 3), f] for d, v, f in rolling_sum(obs, 12)]
            elif tr == "sum12yoy": obs = yoy(rolling_sum(obs, 12), freq)
            elif tr == "qavg_yoy":   # quarterly average of a monthly level (complete quarters only), then % on a year earlier
                qs = {}
                for d, v, f in obs: qs.setdefault(q_of(d), []).append(v)
                obs = yoy([[d, sum(v) / 3, 0] for d, v in sorted(qs.items()) if len(v) == 3], "Q")
            start = DAILY_START if freq == "D" else START
            obs = [[d, round(v, 4), f] for d, v, f in obs if d >= start]
            if freq == "Q": obs = [[q_of(d), v, f] for d, v, f in obs]   # BdE dates quarters by their last month
            if not obs: raise ValueError("sin observaciones")
            if extra.get("source"): sname = extra["source"]
            if src in ("ecb", "ecfin", "oecd"): check_primary_transition(previous.get(sid), obs, sname, sid)
            if src == "cct":
                check_primary_transition(previous.get(sid), obs, sname, sid)
                if previous.get(sid) and previous[sid].get("data"):
                    obs = [o for o in previous[sid]["data"] if o[0] < obs[0][0]] + obs
            if sid in ("sal_ord", "sal_etcl", "clab_etcl") and previous.get(sid):
                reference = dict(previous[sid], source="verificación ETCL pendiente")
                check_primary_transition(reference, obs, sname, sid)
            if sid in ('cred_dudosidad', 'cred_saldo', 'dep_plazo_yoy', 'dep_saldo', 'dep_vista_yoy', 'tipo_cons', 'tipo_dep_hog', 'tipo_dep_snf', 'tipo_dep_vista', 'tipo_snf', 'tipo_viv_saldo'): check_primary_transition(previous.get(sid), obs, sname, sid)
            if sid in ('aux_cr_cons', 'aux_cr_prod', 'aux_cr_viv', 'aux_du_cons', 'aux_du_prod', 'aux_du_viv', 'cred_cons_yoy', 'cred_prod_yoy', 'nop_cons', 'nop_snf', 'nop_viv', 'nop_cons_yoy', 'nop_snf_yoy', 'nop_viv_yoy'): check_primary_transition(previous.get(sid), obs, sname, sid)
            series[sid] = dict(label=label, unit=unit, freq=freq, block=block, source=sname, code=code,
                               table=src.split(":")[1] if ":" in src else None, original_name=name, link=link,
                               transform=tr, note=extra.get("note"), data=obs)
            if sid == "euribor12_daily": series[sid]["link"] = "https://www.bde.es/webbe/es/estadisticas/compartido/datos/csv/ti_1_7.csv"
            if freq == "M": series[sid].update(agg_monthly(sid, obs, level, tr))
            print(f"ok  {sid:14s} {obs[-1][0]}  {obs[-1][1]}")
        except Exception as e:
            errors.append(f"{sid} ({code}): {e}"); print("ERR", sid, e)
            if sid in previous: series[sid] = previous[sid]; print("    kept previous data for", sid)

    # ---- annual figures for the quarterly tables (complete years only)
    #   idx: growth of the annual average of the INE volume index (code = growth-series code - 2)
    #   meanq: average of the four quarters (contributions, LFS rate and level)
    #   growth_of: growth of the annual average of another series' level;  q4: fourth-quarter value
    ANN = {"pib_yoy": "idx", "dn_yoy": "idx", "ch_yoy": "idx", "cp_yoy": "idx", "fbcf_yoy": "idx", "x_yoy": "idx", "m_yoy": "idx",
           "vab_agr": "idx", "vab_ind": "idx", "vab_con": "idx", "vab_ser": "idx",
           "ap_dn": "meanq", "ap_de": "meanq", "paro_epa": "meanq", "ocup_epa": "meanq",
           "ocup_epa_yoy": ("growth_of", "ocup_epa"), "afil_q_yoy": "ss_nsa", "deuda": "q4", "deficit_4q": "q4",
           "ipv": "meanq", "viv_tasado": "meanq", "viv_tasado_yoy": ("growth_of", "viv_tasado"), "viv_credito": "meanq",
           "cred_cons_yoy": "meanq", "cred_prod_yoy": "meanq", "sal_ord": "meanq", "sal_etcl": "meanq", "clab_etcl": "meanq"}
    def by_year(obs, n):
        g = {}
        for d, v, *_ in obs: g.setdefault(d[:4], []).append(v)
        return {y: sum(v) / len(v) for y, v in g.items() if len(v) == n}
    def growth(av): return {y: round(100 * (v / av[str(int(y) - 1)] - 1), 2) for y, v in av.items() if str(int(y) - 1) in av}
    for sid, how in ANN.items():
        if sid not in series: continue
        try:
            if how == "idx": ann = growth(by_year(ine_series("CNTR%d" % (int(series[sid]["code"][4:]) - 2))[1], 4))
            elif how == "meanq": ann = {y: round(v, 2) for y, v in by_year(series[sid]["data"], 4).items()}
            elif how == "ss_nsa": ann = growth(by_year(ss_series("nsa")[1], 12))
            elif how == "q4": ann = {d[:4]: v for d, v, *_ in series[sid]["data"] if d[5:7] == "10"}
            else: ann = growth(by_year(series[how[1]]["data"], 4))
            series[sid]["annual"] = {y: v for y, v in ann.items() if y >= "2015"}
            series[sid]["annual_how"] = how if isinstance(how, str) else how[0]
        except Exception as e:
            errors.append(f"anual {sid}: {e}"); print("ERR annual", sid, e)

    # ---- derived series (stated in 'transform')
    def add(sid, label, unit, freq, block, data, src, how, codes):
        series[sid] = dict(label=label, unit=unit, freq=freq, block=block, source=src, code=codes, table=None,
                           original_name=None, link=None, transform=how, note=None, data=data)
    try:
        gdp4 = rolling_sum(series["pib_nom"]["data"], 4)
        # IGAE quarterly series is cumulative within each year: undo the accumulation first
        ytd = series["deficit_q"]["data"]; flows = []
        for i, (d, v, f) in enumerate(ytd):
            prev = ytd[i - 1] if i else None
            flows.append([d, v - prev[1] if prev and d[5:7] != "01" and prev[0][:4] == d[:4] else v, f])
        deff = rolling_sum(flows, 4)
        add("deficit_4q", "Saldo de las AAPP (suma 4 trimestres)", "% PIB", "Q", "publico",
            ratio_pct(deff, gdp4), "IGAE vía Ministerio de Economía; PIB INE",
            "saldo IGAE desacumulado, suma móvil de 4 trimestres / suma de 4 trimestres del PIB nominal", "770t000000 / CNTR6548")
        # current account: 12-month sum over nominal GDP of the last 4 available quarters
        gdpq = {d: v for d, v, _ in gdp4}
        def over_gdp(m):
            s12 = rolling_sum(series[m]["data"], 12); out = []
            for d, v, f in s12:
                q = q_of(d); keys = sorted(k for k in gdpq if k <= q)
                if keys: out.append([d, round(100 * v / gdpq[keys[-1]], 2), f])
            return out
        add("cc_pib", "Saldo por cuenta corriente (12 meses)", "% PIB", "M", "exterior", over_gdp("cc_m"),
            "Banco de España; PIB INE", "suma de 12 meses / PIB nominal de los 4 últimos trimestres disponibles", "BE_17_1.1")
        add("bys_pib", "Saldo de bienes y servicios (12 meses)", "% PIB", "M", "exterior", over_gdp("bys_m"),
            "Banco de España; PIB INE", "suma de 12 meses / PIB nominal de los 4 últimos trimestres disponibles", "BE_17_1.2")
        tur = rolling_sum(series["tur_ing_m"]["data"], 12)
        add("tur_ing_12m", "Ingresos por turismo (12 meses)", "M€", "M", "exterior", [[d, round(v), f] for d, v, f in tur],
            "Banco de España", "suma móvil de 12 meses", "BE_17_3.3")
        es = {d: v for d, v, _ in series["bono_es"]["data"]}
        add("prima", "Prima de riesgo España − Alemania (10 años)", "pb", "D", "financiero",
            [[d, round(100 * (es[d] - v)), 0] for d, v, _ in series["bono_de"]["data"] if d in es],
            "Banco de España vía Ministerio de Economía", "diferencia diaria × 100", "867320q − 867321q")
    except Exception as e:
        errors.append(f"derivadas: {e}"); print("ERR derived", e)
    # Ratios de dudosidad calculados a partir de series originales trimestrales.
    for sid, numerator, denominator in (("dud_cons", "aux_du_cons", "aux_cr_cons"), ("dud_prod", "aux_du_prod", "aux_cr_prod"), ("dud_viv", "aux_du_viv", "aux_cr_viv")):
        try:
            old = previous.get(sid)
            if not old: raise ValueError("falta la definición previa del ratio")
            values = ratio_pct(series[numerator]["data"], series[denominator]["data"])
            if not values: raise ValueError("sin observaciones comunes")
            check_primary_transition(old, values, "Banco de España", sid)
            add(sid, old["label"], "%", "Q", "credito", values, "Banco de España", "dudosos / saldo de crédito", old["code"])
            series[sid]["annual"] = {d[:4]: v for d,v,*_ in values if d[5:7] == "10" and d[:4] >= "2015"}
            series[sid]["annual_how"] = "q4"
        except Exception as e:
            errors.append(f"{sid}: {e}"); print("ERR", sid, e)
            if sid in previous: series[sid] = previous[sid]
    for sid in ("cc_pib", "bys_pib", "tur_ing_12m"):   # derived monthly series: annual and quarterly figures
        if sid in series: series[sid].update(agg_monthly(sid, series[sid]["data"], None, None))
    if "deficit_4q" in series:   # annual balance = fourth-quarter value of the 4-quarter sum
        series["deficit_4q"]["annual"] = {d[:4]: v for d, v, *_ in series["deficit_4q"]["data"] if d[5:7] == "10" and d[:4] >= "2015"}
        series["deficit_4q"]["annual_how"] = "q4"

    now = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=1)))
    out = dict(meta=dict(title="Monitor macro · Economía española", refreshed=now.strftime("%Y-%m-%d %H:%M"),
                         timezone="Europe/Madrid", sources=["INE", "Banco de España", "Banco Central Europeo", "Comisión Europea (DG ECFIN)", "OCDE", "Ministerio de Trabajo y Economía Social", "Ministerio de Economía, Comercio y Empresa (BDSICE)"],
                         errors=errors, n_series=len(series)), series=series)
    with open(path, "w", encoding="utf-8") as fh: json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    with open("update_log.md", "a", encoding="utf-8") as fh:
        fh.write(f"- {out['meta']['refreshed']}: {len(series)} series" + (f"; errores: {'; '.join(errors)}" if errors else "") + "\n")
    print(f"\n{len(series)} series written to {path}; {len(errors)} errors")
    return 0 if len(series) > 40 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "data.json"))
