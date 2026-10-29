# Tiempo de vuelo (TOF) de muones con la DRS4 — `drs_tof`

Programa de adquisición y análisis para medir la velocidad de los muones
cósmicos por tiempo de vuelo entre barras centelladoras, usando la PSI DRS4
Evaluation Board V5 (serie 2566, tipo 9) conectada por USB a la Raspberry Pi.

- `src/drs_tof.cpp`: adquisición (`drs_tof info`, `drs_tof calib`, `drs_tof medir`).
- `tof_analisis.py`: histogramas de dt31 y ajuste dt31 vs. distancia → v y β.

## Montaje

| Canal | Detector                                                      |
|-------|---------------------------------------------------------------|
| CH1   | barra de **arriba**                                           |
| CH2   | barra del medio, **cruzada** (solo para la coincidencia)      |
| CH3   | barra de **abajo**                                            |

Barras de 4,5 × 4,5 × 46 cm. Los pulsos de los PMT son **negativos**, de
~100–200 mV y 10–20 ns de ancho.

**TOF = dt31 = t(CH3) − t(CH1)**. A β ≈ 1 se esperan ~3,3 ns por metro de
separación, más un offset fijo (cables, PMTs, electrónica) que se elimina
ajustando dt31 en función de la distancia o invirtiendo los detectores.

## 1. Compilar en la Raspberry

```bash
cd muones/grupo_7_jueves
make drs_tof
```

(`make` sin argumentos ahora también compila `drs_tof`.) Hace falta lo mismo
que para `drs_exam`: `libusb-1.0-0-dev` y wxWidgets (ver `raspi.txt`), porque
`DRS.cpp` usa el mutex de wxWidgets.

Para ver las opciones:

```bash
./drs_tof --help
./drs_tof medir --help
```

Se corre con `sudo` (acceso al USB), salvo que estén instaladas las reglas
udev de `41-drs.rules`.

## 2. Calibración (hacerla ANTES de medir)

La DRS4 tiene dos calibraciones, que se guardan en la EEPROM de la placa:

1. **De tensión**: offset y ganancia de cada una de las 1024 celdas de cada canal.
2. **Temporal**: el ancho real de cada celda (no son todas iguales; sin esto
   hay errores de hasta cientos de ps en las diferencias de tiempo). Usa un
   reloj interno de 100 MHz.

> **La calibración temporal vale solo para la frecuencia de muestreo a la que
> se hizo.** Si se mide con otra frecuencia, la librería usa en silencio un eje
> de tiempo uniforme. Por eso `drs_tof medir` se niega a medir si la
> calibración no corresponde a la frecuencia pedida (salvo con `--forzar`).

Paso a paso:

1. Prender la placa y esperar **10–15 minutos** para que se estabilice la temperatura.
2. **Apagar la alta tensión** de los PMTs.
3. **Desconectar todos los cables** de CH1–CH4 (y del EXT TRIG). No tiene que
   llegar ninguna señal a las entradas durante la calibración.
4. Ver el estado actual:
   ```bash
   sudo ./drs_tof info --freq 5
   ```
5. Calibrar (tensión + temporal) a la frecuencia con la que se va a medir:
   ```bash
   sudo ./drs_tof calib --freq 5
   ```
   El programa muestra las instrucciones, espera ENTER y muestra el progreso.
   Tarda del orden de un par de minutos en total. **No desenchufar el USB
   mientras corre** (escribe la EEPROM). Al final relee la EEPROM y muestra:
   - calibración de tensión: VALIDA
   - frecuencia calibrada (EEPROM): 5.1200 GSPS
   - `IsTimingCalibrationValid(): SI`
6. Reconectar los cables y prender la HV.

Notas:

- `--freq 5` en realidad programa **5,12 GSPS** (el PLL de la placa solo
  permite 61,44 GHz / n). La ventana de 1024 celdas dura entonces ~200 ns.
  Siempre se guarda y compara la frecuencia efectiva.
- Opciones: `--solo-tension`, `--solo-tiempo` (rehacer solo la temporal),
  `--si` (no pedir ENTER).
- Recalibrar si la temperatura de la placa cambia mucho (> 5 °C), o si se
  cambia la frecuencia de muestreo.
- Si la calibración temporal da error, la librería de PSI **igual escribe la
  EEPROM**: hay que repetirla (`sudo ./drs_tof calib --solo-tiempo --freq 5`).

## 3. Medir

Ejemplo (barras a 100 cm):

```bash
sudo ./drs_tof medir --dist 100 --nota "CH1 arriba, CH3 abajo, barra media cruzada"
```

Opciones principales (default entre corchetes):

| Opción | Significado |
|---|---|
| `--dist CM` | separación entre barra de arriba y de abajo, **obligatoria** (va al encabezado) |
| `--freq GSPS` | frecuencia de muestreo [5 → 5,12 GSPS] |
| `--delay NS` | demora del trigger [80]. Más demora = el pulso aparece más temprano en la ventana. Satura en 255 × 6,2 ns ≈ 1581 ns |
| `--thr MV` | umbral negativo, en mV [−50]. Es el umbral del trigger por hardware y el que decide si un canal "tiene pulso" |
| `--trigger` | `and` (coincidencia por hardware de los `--canales`) \| `or` \| `ch1` \| `ch2` \| `ch3` [and] |
| `--canales` | canales a leer [1,2,3] |
| `--fraccion` | fracción del CFD [0.5] |
| `--nbase` | mínimo de muestras para la línea de base [30]; la base es la mediana de todo lo anterior al pulso |
| `--nota` | texto libre para el encabezado |
| `--out DIR` | directorio de salida [./medidas/tof/] |
| `--nmax N` | cortar después de N eventos [sin límite] |
| `--rotar-ev N`, `--rotar-seg S` | abrir archivos nuevos cada N eventos [1000] o S segundos [21600 = 6 h] |
| `--sin-ondas` | no guardar las formas de onda |
| `--forzar` | medir aunque la calibración no sea válida (queda anotado en el encabezado) |

Antes de arrancar, el programa imprime la configuración efectiva, incluyendo
una **estimación** de en qué parte de la ventana queda el pulso que dispara
(con `--delay 80` a 5,12 GSPS: ~90 ns de 200 ns). Esa estimación usa un
retardo fijo medido por PSI y puede estar corrida varias decenas de ns, así
que conviene mirar las primeras formas de onda (`*_ondas.txt`) y verificar
que los tres pulsos estén bien adentro de la ventana y lejos del principio
(donde suele haber ruido/ringing). En las mediciones de octubre se ve ese
ruido en los primeros ~45 ns de la ventana, con oscilaciones de hasta
±70 mV: por eso la línea de base se calcula como la mediana de todo el tramo
anterior al pulso y no solo de las primeras muestras.
La coincidencia AND por hardware necesita que las salidas de los comparadores
se superpongan más de ~4–5 ns, lo que se cumple con pulsos de 10–20 ns.

En pantalla aparece una línea por evento: número, hora, tiempo transcurrido,
tasa (ev/h), el último dt31, las amplitudes y el promedio corriente de dt31.
**Ctrl-C** termina cerrando los archivos prolijamente.

### Mediciones largas (horas o días)

Las tasas son muy bajas, así que la medición corre sola durante horas o días.
Usar `tmux` para que no se corte al cerrar la sesión ssh:

```bash
tmux new -s tof
sudo ./drs_tof medir --dist 100 --nota "..."
# despegarse: Ctrl-b y después d     | volver: tmux attach -t tof
```

Los archivos se escriben (flush) después de cada evento, así que un corte de
luz pierde como mucho el evento en curso.

### Tasas esperadas (barras cruzadas, coincidencia triple)

| Separación | Tasa aproximada | Tiempo para ~100 eventos |
|---|---|---|
| 20 cm  | ~25 /h  | ~4 h |
| 50 cm  | ~4 /h   | ~1 día |
| 100 cm | ~1 /h   | ~4 días |
| 150 cm | ~0,5 /h | ~8 días |

El error del promedio de dt31 es σ/√N (σ = resolución por evento, a medir).
Por ejemplo, con σ ≈ 0,5 ns, N = 100 da ~0,05 ns. Conviene empezar con las
distancias cortas para medir σ y planificar el resto.

Plan sugerido: varias separaciones (p. ej. 5, 50, 100, 150 cm), un
histograma de dt31 para cada una, y ajuste lineal dt31 vs. d: la pendiente es
1/v.

## 4. Archivos de salida

Por corrida se generan pares de archivos (con rotación `_idx0`, `_idx1`, …):

```
medidas/tof/tof_d100cm_20261029_153000_idx0_eventos.csv
medidas/tof/tof_d100cm_20261029_153000_idx0_ondas.txt
```

### `*_eventos.csv`

Encabezado de líneas `# clave: valor` (versión del programa, comando, fecha,
serie de la placa, frecuencias nominal/calibrada/real, validez de la
calibración, delay, umbral, trigger, canales, `dist_cm`, fracción CFD, nota),
y después una línea por evento:

```
evento,timestamp,t_cfd1_ns,t_cfd2_ns,t_cfd3_ns,t_le1_ns,t_le2_ns,t_le3_ns,amp1_mV,amp2_mV,amp3_mV,base1_mV,base2_mV,base3_mV,dt31_ns,dt21_ns,dt32_ns
```

- `t_cfd`: tiempo en que el pulso cruza `fraccion × amplitud` en el flanco de
  subida (interpolación lineal entre muestras, sobre el eje temporal
  calibrado **de cada canal**, obtenido con `GetTime` para cada canal).
- `t_le`: cruce del umbral fijo `base + thr` (leading edge), para comparar.
- `amp` = mínimo − línea de base (negativo); `base` = mediana de todas las
  muestras anteriores al pulso (desde la celda 3 hasta 25 muestras antes del
  mínimo, y como mínimo `--nbase` muestras).
- `dt31 = t_cfd3 − t_cfd1`, `dt21`, `dt32`.
- `NaN` si el canal no tiene pulso (amplitud que no llega al umbral o menos
  de 3 muestras por debajo de él) o no se leyó.

### `*_ondas.txt`

Formas de onda crudas, con una columna de tiempo para cada canal:

```
# Columns: t1[ns] u1[mV] t2[ns] u2[mV] t3[ns] u3[mV]
Event #0  ts=2026-10-29 15:31:02.123
  0.0000   -1.2    0.0123    0.4  ...
...
```

## 5. Análisis

Requiere `numpy` y `matplotlib` (`scipy` opcional; si no está usa `numpy.polyfit`).

```bash
# una corrida (se juntan solos los archivos rotados de la misma corrida)
python3 tof_analisis.py medidas/tof/tof_d100cm_20261029_153000_idx*_eventos.csv

# todas las distancias juntas, agrupando por dist_cm, y ajuste dt31 vs d
python3 tof_analisis.py medidas/tof/ --agrupar dist --guardar figuras/

# recalcular el CFD desde las ondas con otra fracción y cortes
python3 tof_analisis.py medidas/tof/ --recalcular-cfd 0.3 --amp-min 60 --dt-rango -10 30
```

Imprime, por archivo y por grupo, N, media, desviación estándar, error de la
media y mediana de dt31; dibuja los histogramas; y si hay al menos dos
distancias distintas ajusta `dt31 = offset + d/v` (ajuste lineal pesado con
los errores de las medias) e imprime 1/v, v, β = v/c, el offset y χ²/ndf.
Con `--no-mostrar` no abre ventanas (útil por ssh); `--guardar DIR` guarda PNGs.

Ojo: no mezclar en el ajuste corridas con los detectores invertidos (ver
abajo): pasarlas por separado.

### Visor en la terminal

`visor_tof.py` es la version para este formato del `visor.py` de drs_exam:
sirve por ssh, sin entorno grafico, y necesita `visor.py` en la misma carpeta.

```bash
python3 visor_tof.py medidas/tof          # recorre ondas y eventos de la carpeta
python3 visor_tof.py medidas/tof -f       # en vivo, mientras corre drs_tof medir
python3 visor_tof.py medidas/tof/tof_d100cm_20261029_153000_idx0_eventos.csv
```

- Sobre un `*_ondas.txt` dibuja los 3 canales (cada uno con su eje de tiempo),
  la linea de umbral y una marca vertical en el `t_cfd` de cada canal (tomado
  del `*_eventos.csv` de la misma corrida), y muestra dt31/dt21/dt32 del evento.
  Teclas: flechas = evento, `1/2/3` = canales, `s` = separar, `+/-` = zoom
  (centrado en el pulso de CH1), `u` = umbral, `c` = marcas de t_cfd.
- Sobre un `*_eventos.csv` muestra el histograma de dt31 (o de dt21, dt32,
  amp1..3 con `h`) con N, media, error, desviacion y mediana, y debajo la tabla
  de eventos. `b`/`B` cambian los bins.
- `Enter` salta entre la onda y la fila de la tabla del mismo evento;
  `[`/`]` cambian de archivo; `f` modo en vivo; `q` salir.

## 6. Chequeos de consistencia

1. **Invertir los detectores de arriba y abajo.** Intercambiar físicamente las
   barras de arriba y de abajo (cada una con su PMT y su cable, que siguen en
   el mismo canal). Si dt31 = offset + d/v en la configuración normal,
   invertido da dt31' = offset − d/v. Entonces:
   - (dt31 − dt31') / 2 = d/v → TOF sin offset;
   - (dt31 + dt31') / 2 = offset (cables + PMTs + electrónica).

   Esto cancela el offset aun midiendo a una sola distancia.
2. **Agregar 1 m de RG-58 en un canal.** La señal en RG-58 viaja a ~0,66 c,
   o sea ~5 ns por metro. Con 1 m extra en CH3, dt31 debe aumentar
   **~5 ns** (o disminuir ~5 ns si se agrega en CH1). Verifica el signo, la
   escala de tiempo y la calibración temporal sin esperar días de muones
   (con `--dist` cualquiera y barras juntas, donde la tasa es alta).

## Problemas frecuentes

- *"La calibración temporal NO es válida a esta frecuencia"*: correr
  `sudo ./drs_tof calib --freq <la misma de medir>`, o medir con la
  frecuencia a la que está calibrada (la imprime el programa y `drs_tof info`).
- *No aparecen eventos*: probar `--trigger or` o `--trigger ch1` para ver si
  llega señal; revisar la HV y el umbral (`--thr`); revisar con `drsosc`.
- *Los pulsos quedan cortados al principio o al final de la ventana*: ajustar
  `--delay` (más delay corre los pulsos hacia el principio de la ventana).
- *No encuentra la placa*: correr con `sudo`, revisar el cable USB.
