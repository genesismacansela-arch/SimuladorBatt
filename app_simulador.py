import streamlit as st 
import pandas as pd    
import numpy as np     
import io
import time
import tempfile 
import os       
import plotly.graph_objects as go
from motor_bess import BESS_Simulator

# ------ FUNCIÓN PARA CACHEAR LA LECTURA DEL ARCHIVO ----------
@st.cache_data(show_spinner="Leyendo archivo...")
def cargar_datos(archivo):
    if archivo.name.endswith('.csv'):
        df = pd.read_csv(archivo, sep=None, engine='python') 
    else:
        df = pd.read_excel(archivo)
    return df

# INTERFAZ GRÁFICA INTERACTIVA CON STREAMLIT

st.set_page_config(page_title="Simulador BESS - Másinteligencia", layout="wide")
st.title("🔋 Simulador de Gestión de Baterías ")
st.markdown("Herramienta modular interactiva para simulación horaria de balances energéticos.")
# --- CSS PERSONALIZADO PARA MÉTRICAS RESPONSIVAS ---
st.markdown("""
<style>
/* 1. Fuente fluida para el valor principal: Mantiene el tamaño original (2.25rem), pero se encoge proporcionalmente hasta 1.2rem si falta espacio */
[data-testid="stMetricValue"] > div {
    font-size: clamp(1.2rem, 2.5vw, 2.25rem) !important; 
    white-space: normal !important;
    overflow-wrap: break-word !important;
}

/* 2. Etiquetas (Delta) fluidas: Se adaptan suavemente sin perder legibilidad */
[data-testid="stMetricDelta"] > div {
    font-size: clamp(0.7rem, 1.2vw, 0.9rem) !important;
    white-space: normal !important;
    overflow-wrap: break-word !important;
}

/* 3. Títulos de las métricas fluidos */
[data-testid="stMetricLabel"] > div {
    font-size: clamp(0.8rem, 1.5vw, 1rem) !important;
    white-space: normal !important;
    overflow-wrap: break-word !important;
}
</style>
""", unsafe_allow_html=True)
# --- BARRA LATERAL: ENTRADA MANUAL DE LAS VARIABLES ---
st.sidebar.header("⚙️ Variables de Simulación")

st.sidebar.subheader("Parámetros Técnicos")
potencia_fv_kwp = st.sidebar.number_input("Potencia FV (kWp)", min_value=0.0, value=2611.2, step=100.0)
perdidas_fv = st.sidebar.number_input("Perdidas FV (%)", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
pot_inversor = st.sidebar.number_input("Potencia inversor (kW)", min_value=0.0, value=2700.0, step=100.0)
tiene_bateria = st.sidebar.checkbox("Sistema con Batería", value=False)
# Declaracion de variables operativas para motor 
coste_ciclo = 0.0 
precio_min_descarga = 0.0
precio_max_carga = 0.0
deg_ciclo = 0.0
descarga_red = False
permitir_carga_red = False

if tiene_bateria:
    cap_bateria = st.sidebar.number_input("Capacidad batería (kWh)", min_value=0.0, value=4600.0, step=10.0)
    max_ciclos = st.sidebar.number_input("Número máximo de ciclos diarios", min_value=0.0, value=1.0, step=0.1)
    st.sidebar.subheader("Estado de Carga y Rendimiento")
    soc_minimo = st.sidebar.number_input("SOC mínimo (%)", min_value=0.0, value=10.0, max_value=100.0, step=10.0) / 100.0
    soc_maximo = st.sidebar.number_input("SOC máximo (%)", min_value=0.0, value=100.0, max_value=100.0, step=10.0) / 100.0
    soc_inicial = st.sidebar.number_input("SOC inicial (%)", min_value=0.0, value=10.0, max_value=100.0, step=10.0) / 100.0
    rend_carga = st.sidebar.number_input("Rendimiento carga (%)", min_value=0.0, value=100.0, max_value=100.0, step=10.0) / 100.0
    rend_descarga = st.sidebar.number_input("Rendimiento descarga (%)", min_value=0.0, value=100.0, max_value=100.0, step=10.0) / 100.0
    
coste_ciclo = 0.0 # Valor por defecto por seguridad si no hay batería
if tiene_bateria:
    st.sidebar.subheader("Parámetros Económicos")
    deg_ciclo = st.sidebar.number_input("Degradación por ciclo (%/ciclo)", value=0.00280, format="%.5f")
    # Coste del ciclo de manera independiente de los checkboxes para poder usarlo abajo
    
st.sidebar.subheader("Restricciones Operativas")
permitir_inyeccion = st.sidebar.checkbox("Permitir inyección a red", value=False)
if tiene_bateria:
    descarga_red = st.sidebar.checkbox("Programar descargas segun precio de mercado", value=False)
    if descarga_red:
        coste_ciclo = st.sidebar.number_input("Coste ciclo batería (€/MWh)", value=0.0, step=1.0)
        limite_real_ac = float(coste_ciclo / rend_descarga) if rend_descarga > 0 else float(coste_ciclo)
        precio_min_descarga = st.sidebar.number_input(
            "Precio mínimo para descargar (€/MWh)", 
            min_value=limite_real_ac, 
            value=limite_real_ac, 
            step=1.0
        )
    permitir_carga_red = st.sidebar.checkbox("Permitir carga desde red", value=False)
    if permitir_carga_red:
        precio_max_carga = st.sidebar.number_input("Precio máximo para cargar desde red (€/MWh)", value=0.0, step=1.0)

# ------------------------------------------------------------------------------------------------------------------------------------------------------------------        

# --- ZONA PRINCIPAL: ENTRADA DEL ARCHIVO ---

@st.cache_data(show_spinner=False)
def ejecutar_motor_optimizado(df_inputs, permitir_inyeccion, tiene_bateria, cap_bateria, pot_inversor, soc_maximo, soc_minimo,
                              soc_inicial, rend_carga, rend_descarga, potencia_fv_kwp, perdidas_fv,max_ciclos, descarga_red,
                              precio_min_descarga, permitir_carga_red, precio_max_carga, coste_ciclo, deg_ciclo):
    # Ejecución del motor físico cacheado aplicando la potencia FV del panel de parametros
    simulador = BESS_Simulator(
        permitir_inyeccion=permitir_inyeccion,
        tiene_bateria=tiene_bateria,
        capacidad_bateria_kwh=cap_bateria if tiene_bateria else 0.0, 
        potencia_inversor_kw=pot_inversor,
        soc_max=soc_maximo if tiene_bateria else 1.0,
        soc_min=soc_minimo if tiene_bateria else 0.0,
        soc_inicial=soc_inicial if tiene_bateria else 0.0,
        rend_carga=rend_carga if tiene_bateria else 1.0,
        rend_descarga=rend_descarga if tiene_bateria else 1.0,
        max_ciclos=max_ciclos if tiene_bateria else 1,
        # Variables gestion operativa
        descarga_red=descarga_red if tiene_bateria else False,
        precio_min_descarga=precio_min_descarga if tiene_bateria else 0.0,
        permitir_carga_red=permitir_carga_red if tiene_bateria else False,
        precio_max_carga=precio_max_carga if tiene_bateria else 0.0,
        coste_ciclo=coste_ciclo if tiene_bateria else 0.0,
        deg_ciclo=deg_ciclo if tiene_bateria else 0.0
    )
    df_resultados = simulador.simular_balance_fisico(df_inputs, potencia_fv_kwp, perdidas_fv)
    kpis_bateria = simulador.kpis

    
    # ------ CONSTRUCCION DEL DATAFRAME ---------
    df_final = pd.DataFrame()
    
    # Evitar la unión con la hora o formatos datetime (REVISAR SI TIMESTAMP PARA APIs)
    if 'Fecha_Raw' in df_resultados.columns:
        if pd.api.types.is_datetime64_any_dtype(df_resultados['Fecha_Raw']):
            df_final['FECHA'] = df_resultados['Fecha_Raw'].dt.strftime('%d/%m/%Y')
        else:
            try:
                df_final['FECHA'] = pd.to_datetime(df_resultados['Fecha_Raw']).dt.strftime('%d/%m/%Y')
            except:
                # Si falla, limpiar quedandose con lo que haya antes del espacio en blanco
                df_final['FECHA'] = df_resultados['Fecha_Raw'].astype(str).apply(lambda x: x.split() if ' ' in x else x)
    else:
        df_final['FECHA'] = "N/A"
        
    #  HORA (Forzado a String entero de 1 a 24 sin decimales .0)
    if 'Hora_Raw' in df_resultados.columns:
        df_final['HORA'] = df_resultados['Hora_Raw'].apply(lambda x: str(int(float(x))) if pd.notnull(x) and str(x).replace('.0','').isdigit() else str(x))
    else:
        df_final['HORA'] = "N/A"
        
    # Inyección y mapeo del resto de columnas en orden
    df_final['PRODUCCION TOTAL (kWh)'] = round(df_resultados['Produccion_Total_kWh'], 2)
    df_final['CONSUMO HORA (kWh)'] = round(df_resultados['Demanda_kWh'], 2)
    df_final['AU DIRECTO (kWh)'] = round(df_resultados['Autoconsumo_Directo_kWh'], 2)
    df_final['SOBRANTE (kWh)'] = round(df_resultados['Sobrante_FV_kWh'], 2)
    
    if tiene_bateria:
        if permitir_carga_red:
            df_final['CARGA BATERIA. FV (kWh)'] = round(df_resultados['Carga_Bat_FV_kWh'], 2)
            df_final['CARGA BAT. RED (kWh)'] = round(df_resultados['Carga_Bat_Red_kWh'], 2)
        df_final['CARGA BATERIA (kWh)'] = round(df_resultados['Carga_Bat_FV_kWh'] + df_resultados['Carga_Bat_Red_kWh'], 2)
        df_final['DESCARGA DE BATERIA (kWh)'] = round(df_resultados['Descarga_Bat_kWh'], 2)
        df_final['SOCV (kWh)'] = round(df_resultados['SOCV_kWh'], 2)
        df_final['SOC (kWh)'] = round(df_resultados['SOC_kWh'], 2)
        df_final['SOC %'] = round(df_resultados['SOC_%'], 2)
        
    df_final['CONSUMO DE RED (kWh)'] = round(df_resultados['Consumo_Red_kWh'], 2)
    
    if permitir_inyeccion:
        df_final['INYECCION A RED (kWh)'] = round(df_resultados['Inyeccion_Red_kWh'], 2)
    else:
        df_final['CURTAILMENT (kWh)'] = round(df_resultados['Curtailment_kWh'], 2)
        
    df_final['AUTOCONSUMO TOTAL (kWh)'] = round(df_resultados['Autoconsumo_Total_kWh'], 2)
    if 'Precio_Red_EUR_MWh' in df_resultados.columns:
        df_final['PRECIO RED (€/MWh)'] = round(df_resultados['Precio_Red_EUR_MWh'], 4)
        
    return df_final, kpis_bateria


archivo_subido = st.file_uploader("📂 Subir plantilla con las columnas (Fecha, Hora, Generacion_FV, Demanda_KWh, Precio_KWh)", type=["csv", "xlsx"])

if archivo_subido is not None:
    try:
        # LLAMAR A AL FUNCIÓN CACHEADA EN LUGAR DE LEERLO DE CERO
        df_inputs_raw = cargar_datos(archivo_subido)
        df_inputs = df_inputs_raw.copy() # Trabaja sobre una copia para no alterar la caché

        df_inputs.columns = df_inputs.columns.str.strip()
        
        
        # ---- RECONOCIMIENTO DE NOMBRES -----
        mapeo_inicial = {}
        for col in df_inputs.columns:
            col_limpia = col.upper().strip().replace(" ", "_").replace("Á", "A").replace("É", "E").replace("Í", "I").replace("Ó", "O").replace("Ú", "U")
            
            if "FECHA" in col_limpia or "DATE" in col_limpia:
                mapeo_inicial[col] = 'Fecha_Raw'
            elif "HORA" in col_limpia or "HOUR" in col_limpia:
                mapeo_inicial[col] = 'Hora_Raw'
            elif "GENERAC" in col_limpia or "PRODUC" in col_limpia or "GEN_" in col_limpia:
                mapeo_inicial[col] = 'Generacion_FV_1kWp'
            elif "DEMANDA" in col_limpia or "CONSUMO" in col_limpia:
                mapeo_inicial[col] = 'Demanda_kWh'
            elif "PRECIO" in col_limpia or "EUR_MWH" in col_limpia:
                mapeo_inicial[col] = 'Precio_Red_EUR_MWh'
                
        # Verificacion de columnas de entrada
        columnas_mapeadas = mapeo_inicial.values()
        if 'Generacion_FV_1kWp' not in columnas_mapeadas or 'Demanda_kWh' not in columnas_mapeadas:
            st.error(f" No se pudieron reconocer las columnas obligatorias de Generación o Demanda. Los encabezados reales de tu archivo son: {list(df_inputs.columns)}")
            st.stop()
            
        df_inputs = df_inputs.rename(columns=mapeo_inicial)
        
        # --- INICIO DEL CRONÓMETRO ---
        tiempo_inicio = time.time()
        
        # Ejecución de la función cacheada
        df_final, kpis_motor = ejecutar_motor_optimizado(
            df_inputs=df_inputs,
            permitir_inyeccion=permitir_inyeccion,
            tiene_bateria=tiene_bateria,
            cap_bateria=cap_bateria if tiene_bateria else 0.0,
            pot_inversor=pot_inversor,
            soc_maximo=soc_maximo if tiene_bateria else 1.0,
            soc_minimo=soc_minimo if tiene_bateria else 0.0,
            soc_inicial=soc_inicial if tiene_bateria else 0.0,
            rend_carga=rend_carga if tiene_bateria else 1.0,
            rend_descarga=rend_descarga if tiene_bateria else 1.0,
            potencia_fv_kwp=potencia_fv_kwp,
            perdidas_fv=perdidas_fv,
            max_ciclos=max_ciclos if tiene_bateria else 1,
            # variables gestion operativa
            descarga_red=descarga_red,
            precio_min_descarga=precio_min_descarga,
            permitir_carga_red=permitir_carga_red,
            precio_max_carga=precio_max_carga,
            coste_ciclo=coste_ciclo,
            deg_ciclo=deg_ciclo
        )
        
        
        # --- FIN DEL CRONÓMETRO ---
        tiempo_fin = time.time()
        tiempo_ejecucion = (tiempo_fin - tiempo_inicio) * 1000 
        
        st.success(f" Simulación procesada correctamente.")
        
#--------------------------------------------------------------------------------------------------
        # --- INTERFAZ VISUAL ---
        
        # Función auxiliar para convertir formato US a formato España
        def formato_es(valor, decimales=0):
            texto_us = f"{valor:,.{decimales}f}"
            return texto_us.replace(",", "X").replace(".", ",").replace("X", ".")
        
        # BLOQUE PRINCIPAL 
        st.header(" Desempeño Energético")
        # Calculos de KPIs
        demanda_anual = df_final['CONSUMO HORA (kWh)'].sum()
        autoconsumo_total = df_final['AUTOCONSUMO TOTAL (kWh)'].sum()
        Consumo_Red = df_final['CONSUMO DE RED (kWh)'].sum()
        soberania = (autoconsumo_total / demanda_anual * 100) if demanda_anual > 0 else 0
        dependencia = 100 - soberania
        
        col1, col2, col3 = st.columns(3)
        
        if tiene_bateria and autoconsumo_total > 0:
            autoconsumo_directo = df_final['AU DIRECTO (kWh)'].sum()
            descarga_bat = df_final['DESCARGA DE BATERIA (kWh)'].sum()
            aporte_FV = (autoconsumo_directo / autoconsumo_total) * 100
            aporte_bat = (descarga_bat / autoconsumo_total) * 100
            delta_autoconsumo = f"{formato_es(aporte_FV, 0)}% directo FV | {formato_es(aporte_bat, 0)}% aporte de batería"
        elif autoconsumo_total > 0:
            delta_autoconsumo = "100% directo FV"
        else:
            delta_autoconsumo = "No se detecto autoconsumo" 
        col1.metric(
            label="Soberanía Energética", 
            value=f"{soberania:.1f}%",
            delta=delta_autoconsumo,
            delta_color="normal" 
        )
        
        if permitir_carga_red and 'CARGA BAT. RED (kWh)' in df_final.columns and Consumo_Red > 0:
            carga_red_bat = df_final['CARGA BAT. RED (kWh)'].sum()
            pct_red_bat = (carga_red_bat / Consumo_Red) * 100
            # % de dependencia general y % exclusivo de la batería
            delta_dependencia = f"{formato_es(dependencia, 1)}% del total | {formato_es(pct_red_bat, 1)}% a batería"
        else:
            delta_dependencia = f"{formato_es(dependencia, 1)}% del total"   
        col2.metric(
            label="Dependencia de Red", 
            value=f"{formato_es(Consumo_Red, 0)} kWh", 
            delta=delta_dependencia, 
            delta_color="normal"
        )
            
              
        # KPI de Excedentes
        total_sobrante_disponible = df_final['SOBRANTE (kWh)'].sum()
        produccion_total = df_final['PRODUCCION TOTAL (kWh)'].sum()
        
        if permitir_inyeccion:
            valor_gestion = df_final['INYECCION A RED (kWh)'].sum()
            label_gestion = "Inyección a Red"
        else:
            valor_gestion = df_final['CURTAILMENT (kWh)'].sum()
            label_gestion = "Curtailment"
            
        porcentaje_gestion = (valor_gestion / total_sobrante_disponible * 100) if total_sobrante_disponible > 0 else 0
        porcentaje_total = (valor_gestion / produccion_total* 100) if produccion_total > 0 else 0
        delta_text = f"{formato_es(porcentaje_total, 1)}% del total | {formato_es(porcentaje_gestion, 1)}% del sobrante"
        col3.metric(
            label=f"Gestión de excedentes: {label_gestion}", 
            value=f"{formato_es(valor_gestion, 0)} kWh", 
            delta=delta_text
        )


        #  METRICAS GLOBALES 
        with st.expander(" Métricas Globales "):
            c1, c2, c3, c4 = st.columns(4)

            c1.metric("Produccion ", f"{formato_es(df_final['PRODUCCION TOTAL (kWh)'].sum())} kWh")
            c2.metric("Consumo Hora", f"{formato_es(df_final['CONSUMO HORA (kWh)'].sum())} kWh")
            c3.metric("Autoconsumo Total", f"{formato_es(df_final['AUTOCONSUMO TOTAL (kWh)'].sum())} kWh")
            c4.metric("Sobrante", f"{formato_es(df_final['SOBRANTE (kWh)'].sum())} kWh")
        

        #  ANÁLISIS DE CICLOS Y DEGRADACIÓN DE BATERIA
        if tiene_bateria and cap_bateria > 0:
            with st.expander(" Análisis de Batería"):
                capacidad_util = cap_bateria * (soc_maximo - soc_minimo)
                aporte_bat_autoconsumo = (descarga_bat / autoconsumo_total * 100) #revisar
                aporte_bat_demanda = (descarga_bat / demanda_anual * 100) if demanda_anual > 0 else 0 #revisar
                energia_descargada_anual_ac = df_final['DESCARGA DE BATERIA (kWh)'].sum()

                # KPIs desde el motor físico
                ciclos = kpis_motor["ciclos_equivalentes"]
                degradacion = kpis_motor["degradacion_anual"]
            
                # Visualizacion
                c_util, c_ciclos, c_deg = st.columns(3)
                
                c_util.metric("Capacidad Útil Real", f"{formato_es(capacidad_util, 0)} kWh", "Limitada por SOC")
                c_ciclos.metric("Ciclos Reales Equivalentes", f"{formato_es(ciclos, 1)} ciclos/año")
                c_deg.metric("Degradación Anual Calculada", f"{formato_es(degradacion, 2)} % anual")
                
                st.divider() 
                
                # los máximos operativos
                ca, cb, cc = st.columns(3)
                ca.metric("Maxima Carga Bateria", f"{formato_es(df_final['CARGA BATERIA (kWh)'].max())} kWh")
                cb.metric("Maxima Descarga Bateria", f"{formato_es(df_final['DESCARGA DE BATERIA (kWh)'].max())} kWh")
                cc.metric("Energía Total Aportada", f"{formato_es(energia_descargada_anual_ac, 0)} kWh")

# ------------------------------------------------------------------------------------------------------------------------------------
        # --- TABLA DE BALANCE MENSUAL Y GRAFICA ----------
        with st.expander("Balance Mensual", expanded=False):
    
                # Asignar meses leyendo las FECHAS del archivo
                df_mensual_temp = df_final.copy()
                
                try:
                    fechas_reales = pd.to_datetime(df_mensual_temp['FECHA'], format='%d/%m/%Y', errors='coerce')
                    df_mensual_temp['Mes_Num'] = fechas_reales.dt.month
                    
                    if df_mensual_temp['Mes_Num'].isnull().any():
                        # Detección automática del inicio
                        fechas_validas = fechas_reales.dropna()
                        if not fechas_validas.empty:
                            fecha_inicio = fechas_validas.iloc[0] # Toma la primera fecha real del archivo
                        else:
                            # Si no hay fechas legibles, toma el año actual
                            fecha_inicio = pd.Timestamp(year=pd.Timestamp.now().year, month=1, day=1) 
                            
                        fechas_genericas = pd.date_range(start=fecha_inicio, periods=len(df_final), freq="h")
                        df_mensual_temp['Mes_Num'] = df_mensual_temp['Mes_Num'].fillna(pd.Series(fechas_genericas.month))
                except Exception:
                    # Respaldo total en caso de error 
                    fechas_reales = pd.to_datetime(df_mensual_temp['FECHA'], format='%d/%m/%Y', errors='coerce').dropna()
                    fecha_inicio = fechas_reales.iloc[0] if not fechas_reales.empty else pd.Timestamp(year=pd.Timestamp.now().year, month=1, day=1)
                    fechas_genericas = pd.date_range(start=fecha_inicio, periods=len(df_final), freq="h")
                    df_mensual_temp['Mes_Num'] = fechas_genericas.month

                df_mensual_temp['Mes_Num'] = df_mensual_temp['Mes_Num'].astype(int)

                col_excedente_origen = 'INYECCION A RED (kWh)' if permitir_inyeccion else 'CURTAILMENT (kWh)'
                col_excedente_destino = 'Inyección a Red (kWh)' if permitir_inyeccion else 'Curtailment (kWh)'

                columnas_origen = [
                    'PRODUCCION TOTAL (kWh)', 
                    'CONSUMO HORA (kWh)', 
                    'AUTOCONSUMO TOTAL (kWh)', 
                    'CONSUMO DE RED (kWh)', 
                    col_excedente_origen
                ]
                columnas_destino = [
                    'Generación (kWh)', 
                    'Demanda (kWh)', 
                    'Autoconsumo (kWh)', 
                    'Consumo de Red (kWh)', 
                    col_excedente_destino
                ]

                df_agrupado = df_mensual_temp.groupby('Mes_Num')[columnas_origen].sum()
                df_agrupado.columns = columnas_destino

                df_agrupado['Penetración de Energía Renovable (%)'] = (df_agrupado['Autoconsumo (kWh)'] / df_agrupado['Demanda (kWh)']) * 100

                nombres_meses = {1: 'Enero', 2: 'Febrero', 3: 'Marzo', 4: 'Abril', 5: 'Mayo', 6: 'Junio', 
                                 7: 'Julio', 8: 'Agosto', 9: 'Septiembre', 10: 'Octubre', 11: 'Noviembre', 12: 'Diciembre'}
                df_agrupado.index = df_agrupado.index.map(nombres_meses)
                df_agrupado.index.name = 'Mes'

                # CÁLCULO DEL TOTAL (para la tabla Y para el gráfico)
                total_row = df_agrupado.sum()
                total_row['Penetración de Energía Renovable (%)'] = (total_row['Autoconsumo (kWh)'] / total_row['Demanda (kWh)']) * 100
                total_row.name = 'TOTAL'
                df_agrupado = pd.concat([df_agrupado, pd.DataFrame(total_row).T])


                # --- GRÁFICO BALANCE INTERACTIVO ---
                st.markdown("<p style='text-align: center; font-weight: bold;' ", unsafe_allow_html=True)
                
                df_plot = df_agrupado.drop('TOTAL', errors='ignore')
                fig = go.Figure()

                fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot['Demanda (kWh)'],
                                         mode='lines+markers', name='Demanda', line=dict(color='#00ff00', width=3)))
                fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot['Consumo de Red (kWh)'],
                                         mode='lines+markers', name='Consumo Red', fill='tozeroy', opacity=0.6, line=dict(color='#BDC3C7')))
                fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot['Autoconsumo (kWh)'],
                                         mode='lines+markers', name='Autoconsumo', fill='tozeroy', opacity=0.6, line=dict(color='#F1C40F')))
                fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot['Generación (kWh)'],
                                         mode='lines+markers', name='Generación', line=dict(color='#F39C12', width=3)))
                col_excedente = 'Inyección a Red (kWh)' if permitir_inyeccion else 'Curtailment (kWh)'
                nombre_leyenda = 'Inyección a Red' if permitir_inyeccion else 'Curtailment'
                
                if col_excedente in df_plot.columns:
                    fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot[col_excedente],
                                             mode='lines+markers', name=nombre_leyenda, fill='tozeroy', opacity=0.6, line=dict(color='#0000FF')))
                fig.update_layout(
                    xaxis_title="",
                    yaxis_title="Energía (kWh)",
                    legend=dict(
                        orientation="h", 
                        yanchor="top", 
                        y=-0.2, 
                        xanchor="center", 
                        x=0.5
                    ),
                    margin=dict(l=0, r=0, t=30, b=50), # margen inferior para que quepa la leyenda
                    height=400
                )
                
                st.plotly_chart(fig, use_container_width=True)

                df_mostrar = df_agrupado.copy()
                for col in df_mostrar.columns:
                    if '%' in col:
                        df_mostrar[col] = df_mostrar[col].apply(lambda x: f"{x:.1f}%" if pd.notnull(x) else "-")
                    else:
                        df_mostrar[col] = df_mostrar[col].apply(lambda x: f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if x > 0.01 else "-")

                st.dataframe(df_mostrar, use_container_width=True)
            

        #  ------------- BALANCE HORARIO --------------
        with st.expander(" Inspeccionar Balance Horario (Primeras 24 horas)"):
            st.dataframe(df_final.head(24).style.format(decimal=",", thousands=".", precision=2))
# --------------------------------------------------------------------------------------------------------------------------------------------------------
            
        # --- BOTONES DE GENERACIÓN DE EXCEL ---
        
        # Crear DataFrame de Configuración (Hoja 1)
        parametros = [
            "Potencia FV (kWp)", "Pérdidas FV (%)", "Potencia Inversor (kW)", 
            "Sistema con Batería", "Permitir Inyección a Red"
        ]
        valores = [
            potencia_fv_kwp, perdidas_fv, pot_inversor, 
            "Sí" if tiene_bateria else "No", "Sí" if permitir_inyeccion else "No"
        ]
        
        if tiene_bateria:
            parametros.extend([
                "Capacidad Batería (kWh)", "Max Ciclos Diarios", "SOC Mínimo (%)", 
                "SOC Máximo (%)", "SOC Inicial (%)", "Rendimiento Carga (%)", 
                "Rendimiento Descarga (%)", "Degradación por Ciclo (%/ciclo)",
                "Programar Descargas según Precio", "Coste Ciclo (€/MWh)", 
                "Precio Mín. Descarga (€/MWh)", "Permitir Carga desde Red", 
                "Precio Máx. Carga (€/MWh)"
            ])
            valores.extend([
                cap_bateria, max_ciclos, soc_minimo * 100, soc_maximo * 100, soc_inicial * 100,
                rend_carga * 100, rend_descarga * 100, deg_ciclo,
                "Sí" if descarga_red else "No",
                coste_ciclo if descarga_red else "N/A",
                precio_min_descarga if descarga_red else "N/A",
                "Sí" if permitir_carga_red else "No",
                precio_max_carga if permitir_carga_red else "N/A"
            ])
            
        df_config = pd.DataFrame({"Parámetro": parametros, "Valor": valores})
        
        # Generar E1, E2 y E4 Horarios
        if tiene_bateria:
            # E4 es la simulación actual activa con todo lo de la barra lateral
            df_e4 = df_final 
            
            # Forzamos E2 (Autoconsumo puro sin cargar ni descargar de red)
            df_e2, _ = ejecutar_motor_optimizado(
                df_inputs=df_inputs, permitir_inyeccion=permitir_inyeccion, tiene_bateria=True,
                cap_bateria=cap_bateria, pot_inversor=pot_inversor, soc_maximo=soc_maximo, soc_minimo=soc_minimo,
                soc_inicial=soc_inicial, rend_carga=rend_carga, rend_descarga=rend_descarga, potencia_fv_kwp=potencia_fv_kwp,
                perdidas_fv=perdidas_fv, max_ciclos=max_ciclos, descarga_red=False, precio_min_descarga=0.0,
                permitir_carga_red=False, precio_max_carga=0.0, coste_ciclo=0.0, deg_ciclo=deg_ciclo
            )
            
            # Forzamos E1 (Sin Batería)
            df_e1, _ = ejecutar_motor_optimizado(
                df_inputs=df_inputs, permitir_inyeccion=permitir_inyeccion, tiene_bateria=False,
                cap_bateria=0.0, pot_inversor=pot_inversor, soc_maximo=1.0, soc_minimo=0.0,
                soc_inicial=0.0, rend_carga=1.0, rend_descarga=1.0, potencia_fv_kwp=potencia_fv_kwp,
                perdidas_fv=perdidas_fv, max_ciclos=1, descarga_red=False, precio_min_descarga=0.0,
                permitir_carga_red=False, precio_max_carga=0.0, coste_ciclo=0.0, deg_ciclo=0.0
            )
        else:
            df_e1 = df_final
            
        # Función para comprimir en balance mensual
        def agrupar_a_mensual(df_horario):
            df_temp = df_horario.copy()
            try:
                fechas = pd.to_datetime(df_temp['FECHA'], format='%d/%m/%Y', errors='coerce')
                df_temp['Mes_Num'] = fechas.dt.month
                
                if df_temp['Mes_Num'].isnull().any():
                    fechas_validas = fechas.dropna()
                    fecha_inicio = fechas_validas.iloc[0] if not fechas_validas.empty else pd.Timestamp(year=pd.Timestamp.now().year, month=1, day=1)
                    
                    df_temp['Mes_Num'] = df_temp['Mes_Num'].fillna(pd.Series(pd.date_range(start=fecha_inicio, periods=len(df_horario), freq="h").month))
            except:
                fechas = pd.to_datetime(df_temp['FECHA'], format='%d/%m/%Y', errors='coerce').dropna()
                fecha_inicio = fechas.iloc[0] if not fechas.empty else pd.Timestamp(year=pd.Timestamp.now().year, month=1, day=1)
                df_temp['Mes_Num'] = pd.date_range(start=fecha_inicio, periods=len(df_horario), freq="h").month
            
            df_temp['Mes_Num'] = df_temp['Mes_Num'].astype(int)
            
            col_exc_origen = 'INYECCION A RED (kWh)' if permitir_inyeccion else 'CURTAILMENT (kWh)'
            col_exc_destino = 'Inyección a Red (kWh)' if permitir_inyeccion else 'Curtailment (kWh)'
            
            cols_origen = ['PRODUCCION TOTAL (kWh)', 'CONSUMO HORA (kWh)', 'AUTOCONSUMO TOTAL (kWh)', 'CONSUMO DE RED (kWh)', col_exc_origen]
            cols_destino = ['Generación (kWh)', 'Demanda (kWh)', 'Autoconsumo (kWh)', 'Consumo de Red (kWh)', col_exc_destino]
            
            df_mes = df_temp.groupby('Mes_Num')[cols_origen].sum()
            df_mes.columns = cols_destino
            df_mes['Penetración de Energía Renovable (%)'] = (df_mes['Autoconsumo (kWh)'] / df_mes['Demanda (kWh)']) * 100
            
            meses_map = {1: 'Enero', 2: 'Febrero', 3: 'Marzo', 4: 'Abril', 5: 'Mayo', 6: 'Junio', 
                         7: 'Julio', 8: 'Agosto', 9: 'Septiembre', 10: 'Octubre', 11: 'Noviembre', 12: 'Diciembre'}
            df_mes.index = df_mes.index.map(meses_map)
            df_mes.index.name = 'Mes'
            
            totales = df_mes.sum()
            totales['Penetración de Energía Renovable (%)'] = (totales['Autoconsumo (kWh)'] / totales['Demanda (kWh)']) * 100
            totales.name = 'TOTAL'
            
            return pd.concat([df_mes, pd.DataFrame(totales).T])

        df_mensual_e1 = agrupar_a_mensual(df_e1)
        if tiene_bateria:
            df_mensual_e2 = agrupar_a_mensual(df_e2) 
            df_mensual_e4 = agrupar_a_mensual(df_e4)

        # --- FUNCIONES PARA IMAGENES EN EL EXCEL ---
        def generar_figura_exportacion(df_plot, titulo):
            fig = go.Figure()
            
            fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot['Consumo de Red (kWh)'], mode='lines+markers', name='Consumo Red', stackgroup='one', line=dict(color='#BDC3C7')))
            fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot['Autoconsumo (kWh)'], mode='lines+markers', name='Autoconsumo', stackgroup='two', line=dict(color='#F1C40F')))
            fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot['Generación (kWh)'], mode='lines+markers', name='Generación', line=dict(color='#F39C12', width=3)))
            col_exc = 'Inyección a Red (kWh)' if permitir_inyeccion else 'Curtailment (kWh)'
            nombre_exc = 'Inyección a Red' if permitir_inyeccion else 'Curtailment'
            if col_exc in df_plot.columns:
                fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot[col_exc], mode='lines+markers', name=nombre_exc, line=dict(color='#0000FF')))
            
            fig.update_layout(
                title=dict(text=titulo, font=dict(size=20)), 
                yaxis_title="Energía (kWh)", 
                xaxis_title="Mes",
                legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="center", x=0.5), 
                # Aumentar los margenes: l=80 (escala Y), b=120 (eje X y leyenda)
                margin=dict(l=80, r=40, t=60, b=120), 
                height=500, # Un poco más alta para absorber el margen
                width=900
            )
            return fig

        # Guardar el primer Excel (Horario completo)
        output_horario = io.BytesIO()
        with pd.ExcelWriter(output_horario, engine='openpyxl') as writer:
            df_config.to_excel(writer, index=False, sheet_name="Configuración")
            df_e1.to_excel(writer, index=False, sheet_name="E1 - Sin Batería")
            if tiene_bateria:
                df_e2.to_excel(writer, index=False, sheet_name="E2 - Con Batería")
                df_e4.to_excel(writer, index=False, sheet_name="E4 - Carga Red") # NUEVA PESTAÑA
        output_horario.seek(0)
        
        # --- TABLA COMPARATIVA AUTOMÁTICA ---
        totales_e1 = df_mensual_e1.loc['TOTAL']
        
        dict_comparativa = {
            "Métrica": totales_e1.index,
            "E1 - Sin Batería": totales_e1.values
        }
        if tiene_bateria:
            dict_comparativa["E2 - Con Batería"] = df_mensual_e2.loc['TOTAL'].values
            dict_comparativa["E4 - Carga Red"] = df_mensual_e4.loc['TOTAL'].values # NUEVA COLUMNA
            
        df_comparativa = pd.DataFrame(dict_comparativa)

        # Guardar el segundo Excel (Resumen Mensual + Comparativa Imagenes)
        output_mensual = io.BytesIO()
        
        # Usamos tempfile para evitar WinError 32
        with tempfile.TemporaryDirectory() as tmpdir:
            with pd.ExcelWriter(output_mensual, engine='openpyxl') as writer:
                df_config.to_excel(writer, index=False, sheet_name="Configuración", float_format="%.2f")
                df_comparativa.to_excel(writer, index=False, sheet_name="Comparativa", startrow=1, startcol=1, float_format="%.2f")
                
                try:
                    from openpyxl.drawing.image import Image as XLImage
                    hoja_graficos = writer.sheets["Comparativa"]
                    fila_actual = len(df_comparativa) + 4 
                    
                    # Gráfica E1
                    fig_e1 = generar_figura_exportacion(df_mensual_e1.drop('TOTAL', errors='ignore'), "Escenario 1: Sin Batería")
                    ruta_e1 = os.path.join(tmpdir, "e1.png")
                    fig_e1.write_image(ruta_e1, format="png", scale=1)
                    hoja_graficos.add_image(XLImage(ruta_e1), f'A{fila_actual}')
                    
                    if tiene_bateria:
                        # Gráfica E2
                        fig_e2 = generar_figura_exportacion(df_mensual_e2.drop('TOTAL', errors='ignore'), "Escenario 2: Con Batería")
                        ruta_e2 = os.path.join(tmpdir, "e2.png")
                        fig_e2.write_image(ruta_e2, format="png", scale=1)
                        hoja_graficos.add_image(XLImage(ruta_e2), f'F{fila_actual}')
                        
                        # Gráfica E4 (Debajo del E1)
                        fila_siguiente = fila_actual + 26
                        fig_e4 = generar_figura_exportacion(df_mensual_e4.drop('TOTAL', errors='ignore'), "Escenario 4: Carga desde Red")
                        ruta_e4 = os.path.join(tmpdir, "e4.png")
                        fig_e4.write_image(ruta_e4, format="png", scale=1)
                        hoja_graficos.add_image(XLImage(ruta_e4), f'A{fila_siguiente}')
                        
                except Exception as ex:
                    st.warning("⚠️ No se pudieron adjuntar los gráficos al Excel.")

                # Tablas mensuales
                df_mensual_e1.round(2).to_excel(writer, sheet_name="E1 - Sin Batería", float_format="%.2f") 
                if tiene_bateria:
                    df_mensual_e2.round(2).to_excel(writer, sheet_name="E2 - Con Batería", float_format="%.2f")
                    df_mensual_e4.round(2).to_excel(writer, sheet_name="E4 - Carga Red", float_format="%.2f")

                # CORRECCIÓN OPENPYXL: Evitar el error de la tupla
                from openpyxl.utils import get_column_letter
                for sheet_name in writer.sheets:
                    ws = writer.sheets[sheet_name]
                    for i, col in enumerate(ws.columns, 1):
                        max_len = max((len(str(cell.value or '')) for cell in col), default=0)
                        col_letter = get_column_letter(i)
                        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)
                    
        output_mensual.seek(0)
        
        # --- INTERFAZ DE BOTONES ---
        st.divider()
        st.markdown("### Exportación de Resultados")
        
        col_btn1, col_btn2 = st.columns(2)
        
        with col_btn1:
            st.download_button(
                label=" Descargar Resumen Mensual ",
                data=output_mensual,
                file_name="Balance_Mensual_Escenarios.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                use_container_width=True
            )
            
        with col_btn2:
            st.download_button(
                label=" Descargar Balance Horario Completo (8760h)",
                data=output_horario,
                file_name="Balance_Horario_Escenarios.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
            
    except Exception as e:
        st.error(f"Error en el procesamiento del archivo: {e}. Comprueba los nombres de las columnas.")
