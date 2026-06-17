import pandas as pd    
import numpy as np     

# clase de Fórmulas BESS de Balance de Energía

class BESS_Simulator:
    def __init__(self, capacidad_bateria_kwh, potencia_inversor_kw, tiene_bateria=True, permitir_inyeccion=True,
                 soc_max=1.0, soc_min=0.1, soc_inicial=0.1, rend_carga=1.0, rend_descarga=1.0, max_ciclos=1.0,
                 cd_rate=1.0, cc_rate=1.0, descarga_red=False, precio_min_descarga=0.0, permitir_carga_red=False,
                 precio_max_carga=0.0, coste_ciclo=0.0, deg_ciclo=0.0):
        self.tiene_bateria = tiene_bateria
        self.permitir_inyeccion = permitir_inyeccion
        self.capacidad_max = capacidad_bateria_kwh 
        self.potencia_inv = potencia_inversor_kw 
        self.soc_max_kwh = capacidad_bateria_kwh * soc_max 
        self.soc_min_kwh = capacidad_bateria_kwh * soc_min 
        self.soc_inicial_kwh = capacidad_bateria_kwh * soc_inicial 
        self.rend_carga = rend_carga
        self.rend_descarga = rend_descarga
        self.max_ciclos = max_ciclos
        self.cd_rate = cd_rate
        self.cc_rate = cc_rate
        # Si la batería carga a 0.5C (Lado DC), la red (Lado AC) tiene que empujar más energía por las pérdidas
        self.limite_carga_ac_kw = (self.capacidad_max * self.cc_rate) / self.rend_carga if self.rend_carga > 0 else 0.0
        # Si la batería descarga a 1C (Lado DC), llega menos energía a la red por las pérdidas
        self.limite_descarga_ac_kw = (self.capacidad_max * self.cd_rate) * self.rend_descarga

        # Económicos y Degradación
        self.descarga_red = descarga_red
        self.precio_min_descarga = precio_min_descarga
        self.permitir_carga_red = permitir_carga_red
        self.precio_max_carga = precio_max_carga
        self.coste_ciclo = coste_ciclo
        self.deg_ciclo = deg_ciclo
        
        # Diccionario para almacenar métricas y pasarlas al Excel
        self.kpis = {}
        
    def simular_balance_fisico(self, df_inputs, potencia_fv_kwp, perdidas_fv):
        self.df = df_inputs.copy() 
        
        # --- CÁLCULOS VECTORIZADOS ---
        # Producción Total = Generación horaria * Potencia FV (kWp) configurada manualmente
        self.df['Produccion_Total_kWh'] = np.minimum(((1 - perdidas_fv / 100) * (self.df['Generacion_FV_1kWp'] * potencia_fv_kwp)), self.potencia_inv)
        # AU Directo = SI(Produccion total < Consumo Hora; Produccion total; Consumo Hora) --> MIN
        self.df['Autoconsumo_Directo_kWh'] = np.minimum(self.df['Produccion_Total_kWh'], self.df['Demanda_kWh'])
        # Sobrante = MAX(Produccion total - AU Directo; 0)
        self.df['Sobrante_FV_kWh'] = np.maximum(self.df['Produccion_Total_kWh'] - self.df['Autoconsumo_Directo_kWh'], 0)
        # Demanda Residual: Energía que queda descubierta para la batería o la red
        self.df['Demanda_Residual_kWh'] = np.maximum(self.df['Demanda_kWh'] - self.df['Autoconsumo_Directo_kWh'], 0)
        
        
        # --- CONTROL SI NO HAY BATERÍA (Escenario 1) ---
        if not self.tiene_bateria:
            self.df['Carga_Bat_FV_kWh'] = 0.0
            self.df['Carga_Bat_Red_kWh'] = 0.0 #E4
            self.df['Descarga_Bat_kWh'] = 0.0
            self.df['SOC_kWh'] = 0.0
            self.df['SOCV_kWh'] = 0.0
            self.df['SOC_%'] = 0.0
            self.df['Consumo_Red_kWh'] = self.df['Demanda_Residual_kWh']
            if self.permitir_inyeccion: 
                self.df['Inyeccion_Red_kWh'] = self.df['Sobrante_FV_kWh']
                self.df['Curtailment_kWh'] = 0.0
            else:
                self.df['Inyeccion_Red_kWh'] = 0.0
                self.df['Curtailment_kWh'] = self.df['Sobrante_FV_kWh']
                
            self.df['Autoconsumo_Total_kWh'] = self.df['Autoconsumo_Directo_kWh']
            return self.df

        else:    
        # --- ITERACIÓN TEMPORAL DE LA BATERÍA (Escenario 2 - Bateria Autoconsumo Puro) ---
            sobrante = self.df['Sobrante_FV_kWh'].values 
            demanda_res = self.df['Demanda_Residual_kWh'].values 
            # autoconsumo directo para calcular el embudo del inversor
            autoconsumo_directo = self.df['Autoconsumo_Directo_kWh'].values
            # Extraer la hora para saber cuándo cambiar de día
            horas_raw = self.df['Hora_Raw'].astype(str).values if 'Hora_Raw' in self.df.columns else []
            n_horas = len(sobrante)
            # Integracion precios parte economica
            precios = self.df['Precio_Red_EUR_MWh'].values if 'Precio_Red_EUR_MWh' in self.df.columns else np.zeros(len(sobrante))
        
            carga_bat_fv = np.zeros(n_horas)
            carga_bat_red = np.zeros(n_horas) #E4
            descarga_bat = np.zeros(n_horas)
            soc_array = np.zeros(n_horas)
            soc_actual = self.soc_inicial_kwh

            # Control de ciclos
            descarga_dc_diaria_acumulada = 0.0
            limite_descarga_dc_diario = self.max_ciclos * self.capacidad_max

            # UMBRAL OPERATIVO (Proteccion de la batería si no vale la pena descargar por el precio)
            umbral_operativo = max(self.precio_min_descarga, (self.coste_ciclo / self.rend_descarga) if self.rend_descarga > 0 else 0)
        
            for i in range(n_horas):
                hora_del_dia = i % 24
                if hora_del_dia == 0: # nuevo día
                    descarga_dc_diaria_acumulada = 0.0
                    top_horas_carga = []
                    if self.permitir_carga_red: # Escenario 4
                        if self.potencia_inv > 0:
                            # Ventana de tiempo dinámica indexada a max_ciclos y tasas de carga
                            energia_total_objetivo = self.max_ciclos * self.capacidad_max
                            energia_top1 = self.potencia_inv * 0.8  # Mejor hora rinde al 80%
                            
                            if energia_total_objetivo <= energia_top1:
                                n_horas_top = 1
                            else:
                                energia_faltante = energia_total_objetivo - energia_top1
                                energia_resto_por_hora = self.potencia_inv * 0.3  # Horas secundarias al 30%
                                horas_extra = energia_faltante / energia_resto_por_hora
                                n_horas_top = 1 + int(np.ceil(horas_extra))
                            
                            # Acotar la ventana entre 1 y 24 horas máximo al día
                            n_horas_top = min(24, max(1, n_horas_top))
                        else:
                            n_horas_top = 0

                        # Extraer y ordenar precios del bloque diario de 24h
                        fin_dia = min(i + 24, n_horas)
                        precios_dia = precios[i:fin_dia]
                        horas_validas = [h for h in range(fin_dia - i) if precios_dia[h] <= self.precio_max_carga]
                        horas_ordenadas = sorted(horas_validas, key=lambda x: precios_dia[x])
                        top_horas_carga = horas_ordenadas[:n_horas_top]
                    
                    
            # Carga batería: MIN(Sobrante; Hueco_disponible; Potencia_Inversor) con rendimientos aplicados
                hueco_bateria_quimico = self.soc_max_kwh - soc_actual # Estado bateria anterior menos actual 
                max_carga_AC = min(sobrante[i], self.potencia_inv, self.limite_carga_ac_kw) # se limita con el inversor y el C-rate 
                carga_real_quimica = min(max_carga_AC * self.rend_carga, hueco_bateria_quimico) # se entrelaza
                soc_actual += carga_real_quimica
                carga_bat_fv[i] = carga_real_quimica / self.rend_carga if self.rend_carga > 0 else 0.0

            # CARGA DESDE LA RED (Escenario 4)
                carga_red_AC = 0.0
                if self.permitir_carga_red and (hora_del_dia in top_horas_carga):
                    if hora_del_dia == top_horas_carga[0]: 
                        fe = 0.8
                    else: 
                        fe = 0.3    
                    # Recalcular hueco químico tras haber inyectado el sol de ESTA hora
                    hueco_bateria_quimico = self.soc_max_kwh - soc_actual
                    if hueco_bateria_quimico > 0:
                        # --- RADAR SOLAR (mira al futuro para no desperdiciar carga con energia solar) ---
                        fin_de_hoy = min(i + (24 - hora_del_dia), n_horas)
                        sobrante_futuro_hoy = np.sum(sobrante[i+1 : fin_de_hoy])
                        hueco_permitido_red = max(0, hueco_bateria_quimico - (sobrante_futuro_hoy * self.rend_carga))
                        
                        if hueco_permitido_red > 0:
                            # --- RADAR DE PRECIOS (mira al futuro para la protección de la mejor hora) ---
                            energia_reservada_futura = 0.0
                            for futura_hora in top_horas_carga:
                                # Si hay una hora de carga planificada para más tarde...
                                if futura_hora > hora_del_dia:
                                    # ...y tiene un precio menor que la hora actual, guardar obligatoriamente su hueco
                                    if precios_dia[futura_hora] < precios_dia[hora_del_dia]:
                                        fe_futura = 0.8 if futura_hora == top_horas_carga[0] else 0.3
                                        energia_reservada_futura += (self.potencia_inv * fe_futura)
                            
                            # Modifica el espacio útil disponible para esta hora restando la reserva futura
                            hueco_permitido_red = max(0, hueco_permitido_red - (energia_reservada_futura * self.rend_carga))
                            
                            if hueco_permitido_red > 0:
                                # cuato espacio le queda al puente del inversor
                                potencia_inv_remanente = max(0, self.potencia_inv - carga_bat_fv[i])
                                # Cuanta velocidad quimica (C-Rate) queda libre tras haber cargado el sol?
                                c_rate_remanente = max(0, self.limite_carga_ac_kw - carga_bat_fv[i])
                                # El cuello de botella real es el menor entre el inversor, la capacidad y el C-Rate restante
                                potencia_max_fisica = min(potencia_inv_remanente, c_rate_remanente)
                                # factor financiero (0.8 o 0.3) al cuello de botella
                                limite_red_neta_AC = potencia_max_fisica * fe
                                
                                # Ejecución final
                                carga_red_AC = min(limite_red_neta_AC, hueco_permitido_red / self.rend_carga)
                                carga_real_quimica_red = carga_red_AC * self.rend_carga
                                
                                soc_actual += carga_real_quimica_red
                                carga_bat_red[i] = carga_red_AC

                # ¿Está el inversor en su ventana de horas baratas programadas para cargar?
                es_ventana_de_carga = self.permitir_carga_red and (hora_del_dia in top_horas_carga)
                descarga_AC = 0.0
                
                # --- LÓGICA ESCENARIO 3: REPARTO VIRTUAL DE PRIORIDAD ABSOLUTA (GREEDY) ---
                es_rentable_descargar = True
                # Cuanto espacio libre le queda al inversor 
                potencia_inv_remanente_descarga = max(0, self.potencia_inv - autoconsumo_directo[i])
                limite_financiero_descarga = potencia_inv_remanente_descarga # Por defecto, el límite físico
                
                if self.descarga_red:
                    fin_dia = min(i + (24 - hora_del_dia), n_horas)
                    precios_restantes = precios[i:fin_dia]
                    demanda_restante = demanda_res[i:fin_dia]
                    sobrante_restante = sobrante[i:fin_dia] # Para el radar solar
                    autoconsumo_restante = autoconsumo_directo[i:fin_dia]
                    
                    coste_minimo_ciclo_kWh = (self.coste_ciclo / 1000) / self.rend_descarga if self.rend_descarga > 0 else 0
                    umbral_rentabilidad_kWh = max(self.precio_min_descarga / 1000, coste_minimo_ciclo_kWh)
                    precio_actual_kWh = precios[i] / 1000
                    
                    if precio_actual_kWh < umbral_rentabilidad_kWh:
                        es_rentable_descargar = False 
                        limite_financiero_descarga = 0.0
                    else:
                        # 1. RADAR COMPLETO: ¿Cuánta energía tendremos HOY? (Química actual + Sol que va a sobrar)
                        energia_disp_quimica = max(0, soc_actual - self.soc_min_kwh)
                        sol_futuro_quimica = np.sum(sobrante_restante[1:]) * self.rend_carga # Sol desde la hora siguiente
                        
                        credito_diario = max(0, limite_descarga_dc_diario - descarga_dc_diaria_acumulada)
                        
                        # Energía total que podemos repartir en la matriz virtual
                        energia_total_virtual_quimica = min(energia_disp_quimica + sol_futuro_quimica, credito_diario)
                        energia_total_virtual_AC = energia_total_virtual_quimica * self.rend_descarga
                        
                        # 2. Identificamos TODAS las horas rentables restantes (incluyendo la actual, índice 0)
                        horas_rentables_idx = [
                            h for h in range(len(precios_restantes))
                            if (precios_restantes[h] / 1000) >= umbral_rentabilidad_kWh and demanda_restante[h] > 0
                        ]
                        
                        # 3. ORDEN ESTRICTO: De la hora más cara a la más barata
                        horas_ordenadas_vip = sorted(horas_rentables_idx, key=lambda x: precios_restantes[x], reverse=True)
                        
                        # 4. Reparto virtual de la energía ("Greedy Allocation")
                        energia_virtual_AC = energia_total_virtual_AC
                        asignacion_hora_actual = 0.0
                        
                        for h_idx in horas_ordenadas_vip:
                            if energia_virtual_AC <= 0.0001: # Si se acabó la energía virtual, se deja de repartir
                                break
                                
                            potencia_inv_h_futura = max(0, self.potencia_inv - autoconsumo_restante[h_idx])
                            tope_descarga_h_futura = min(potencia_inv_h_futura, self.limite_descarga_ac_kw) # Freno Químico C-rate
                            necesidad_h = min(demanda_restante[h_idx], tope_descarga_h_futura)
                            asignacion = min(necesidad_h, energia_virtual_AC)
                            energia_virtual_AC -= asignacion
                            
                            # Si en este reparto estricto le tocó el turno a la hora ACTUAL (índice 0)
                            if h_idx == 0:
                                asignacion_hora_actual = asignacion
                                
                        # 5. Veredicto final para la hora actual
                        if asignacion_hora_actual > 0:
                            es_rentable_descargar = True
                            limite_financiero_descarga = asignacion_hora_actual # ¡EL CANDADO PARA LA FUGA!
                        else:
                            es_rentable_descargar = False
                            limite_financiero_descarga = 0.0

                # --- EJECUCIÓN FISICA DE LA DESCARGA ---
                if (carga_red_AC == 0.0) and not es_ventana_de_carga and es_rentable_descargar:
                    energia_disponible_quimica = max(0, soc_actual - self.soc_min_kwh)
                    # Calcular cuánto "crédito" de descarga le queda hoy a la batería
                    credito_descarga_dc_diario = max(0, limite_descarga_dc_diario - descarga_dc_diaria_acumulada)
                    # La energía que se puede sacar es el mínimo entre lo que tiene la celda y su crédito diario
                    energia_disponible_quimica_limitada = min(energia_disponible_quimica, credito_descarga_dc_diario)
                    energia_disponible_AC = energia_disponible_quimica_limitada * self.rend_descarga
                    # Cálculo base de la física
                    descarga_AC = min(demanda_res[i], energia_disponible_AC, potencia_inv_remanente_descarga, self.limite_descarga_ac_kw)
                    # candado financiero si el escenario 3 o 5 están activos
                    if self.descarga_red:
                        descarga_AC = min(descarga_AC, limite_financiero_descarga)
                    # Calculo desgaste químico real de la descarga y sumar al contador (SOC)
                    energia_extraida_dc = (descarga_AC / self.rend_descarga) if self.rend_descarga > 0 else 0.0
                    soc_actual -= energia_extraida_dc
                    descarga_dc_diaria_acumulada += energia_extraida_dc
                
                descarga_bat[i] = descarga_AC
                # SOC final de la hora
                soc_array[i] = soc_actual
            
        # --- VOLCADO DE RESULTADOS DEL BUCLE ---
            self.df['Carga_Bat_FV_kWh'] = carga_bat_fv
            self.df['Carga_Bat_Red_kWh'] = carga_bat_red
            self.df['Descarga_Bat_kWh'] = descarga_bat
            self.df['SOC_kWh'] = soc_array
            self.df['SOCV_kWh'] = soc_array # SOCV es igual a SOC en la lógica física base, se deberia diferenciar la virtual de la real
        
        # --- CÁLCULOS FINALES VECTORIZADOS ---
            self.df['SOC_%'] = self.df['SOC_kWh'] / self.capacidad_max * 100
            self.df['Consumo_Red_kWh'] = np.maximum(self.df['Demanda_kWh'] - self.df['Autoconsumo_Directo_kWh'] - self.df['Descarga_Bat_kWh'], 0) + self.df['Carga_Bat_Red_kWh']
            self.df['Autoconsumo_Total_kWh'] = self.df['Autoconsumo_Directo_kWh'] + self.df['Descarga_Bat_kWh']
        # separacion de excedentes (inyeccion o curtailment)
            excedente_final = np.maximum(self.df['Sobrante_FV_kWh'] - self.df['Carga_Bat_FV_kWh'], 0)
            if self.permitir_inyeccion:
                self.df['Inyeccion_Red_kWh'] = excedente_final
                self.df['Curtailment_kWh'] = 0.0
            else:
                self.df['Inyeccion_Red_kWh'] = 0.0
                self.df['Curtailment_kWh'] = excedente_final

            # KPIs bateria
            energia_descargada_anual_dc = (self.df['Descarga_Bat_kWh'] / self.rend_descarga).sum() if self.rend_descarga > 0 else 0.0
            ciclos_equivalentes = energia_descargada_anual_dc / self.capacidad_max if self.capacidad_max > 0 else 0.0
            degradacion_anual = ciclos_equivalentes * self.deg_ciclo
            
            self.kpis = {
                "ciclos_equivalentes": ciclos_equivalentes,
                "degradacion_anual": degradacion_anual
            }
        
            return self.df

