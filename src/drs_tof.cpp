/********************************************************************\

  Nombre:   drs_tof.cpp
  Grupo 7 (jueves) - Laboratorio 5, FCEN-UBA

  Medicion de la velocidad de muones cosmicos por tiempo de vuelo (TOF)
  entre barras centelladoras leidas con PMTs y digitalizadas con la
  placa PSI DRS4 Evaluation Board V5.

  Subcomandos:
     drs_tof info                 informacion de la placa y de su calibracion
     drs_tof calib  [opciones]    calibracion de tension + calibracion temporal
     drs_tof medir  [opciones]    adquisicion de eventos (ver drs_tof medir --help)

  Para cada evento se guarda una linea en <run>_eventos.csv con los tiempos
  CFD y de umbral (leading edge) de cada canal, amplitudes, lineas de base y
  las diferencias de tiempo dt31, dt21, dt32. Opcionalmente se guardan las
  formas de onda crudas en <run>_ondas.txt (con el eje temporal calibrado
  de CADA canal).

  Compilar (en la Raspberry):   make drs_tof
  Ver README_TOF.md para el procedimiento completo.

\********************************************************************/

#include <math.h>
#include <ctype.h>
#include <cmath>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <signal.h>

#include <algorithm>
#include <chrono>
#include <ctime>
#include <string>
#include <sstream>
#include <iomanip>
#include <vector>

#ifdef _MSC_VER
  #include <windows.h>
  #include <direct.h>   // _mkdir
  #include <io.h>       // _isatty
#elif defined(OS_LINUX) || defined(OS_DARWIN)
  #include <unistd.h>
  #include <ctype.h>
  #include <sys/ioctl.h>
#endif

#include <sys/stat.h>
#include <errno.h>

#include "DRS.h"

/*==================== Constantes ====================*/

static const char *kVersion = "drs_tof 1.0 (grupo 7, Labo 5 UBA)";

static const int    kNBins      = 1024;   // celdas del DRS4 por canal

// Demora del trigger: el firmware usa ticks de LUT (ver DRS.cpp, SetTriggerDelayNs)
static const double kLutDelayNuevo = 6.2; // ns por tick, firmware >= 17147
static const double kLutDelayViejo = 2.1; // ns por tick, firmware anterior
static const int    kMaxTicks      = 255;

/*==================== Estado global (Ctrl-C) ====================*/

static volatile sig_atomic_t g_stop = 0;

static void manejador_senal(int sig)
{
   g_stop = 1;
   // un segundo Ctrl-C mata el programa de inmediato
   signal(sig, SIG_DFL);
}

static void dormir_ms(int ms)
{
#ifdef _MSC_VER
   Sleep(ms);
#else
   usleep(ms * 1000);
#endif
}

static bool stdout_es_terminal()
{
#ifdef _MSC_VER
   return _isatty(_fileno(stdout)) != 0;
#else
   return isatty(fileno(stdout)) != 0;
#endif
}

/*==================== Utilidades de directorios / tiempo ====================*/

static bool make_one_dir(const std::string &path)
{
#ifdef _MSC_VER
   if (_mkdir(path.c_str()) == 0 || errno == EEXIST) return true;
#else
   if (mkdir(path.c_str(), 0755) == 0 || errno == EEXIST) return true;
#endif
   return false;
}

// Crea un directorio y sus padres (como `mkdir -p`)
static bool make_dirs(const std::string &path)
{
   if (path.empty()) return true;
   for (size_t pos = 1; pos <= path.size(); pos++) {
      if (pos == path.size() || path[pos] == '/' || path[pos] == '\\') {
         std::string sub = path.substr(0, pos);
         if (sub.empty() || sub == "." || sub == "..") continue;
         if (!make_one_dir(sub)) {
            perror(("ERROR: no se pudo crear el directorio " + sub).c_str());
            return false;
         }
      }
   }
   struct stat st;
   if (stat(path.c_str(), &st) != 0 || !(st.st_mode & S_IFDIR)) {
      fprintf(stderr, "ERROR: %s no es un directorio\n", path.c_str());
      return false;
   }
   return true;
}

static std::tm hora_local(std::time_t t)
{
   std::tm tm{};
#if defined(_MSC_VER)
   localtime_s(&tm, &t);
#else
   localtime_r(&t, &tm);
#endif
   return tm;
}

// "YYYY-MM-DD HH:MM:SS.mmm"
static std::string now_timestamp()
{
   using namespace std::chrono;
   auto now = system_clock::now();
   auto t   = system_clock::to_time_t(now);
   auto ms  = duration_cast<milliseconds>(now.time_since_epoch()) % 1000;
   std::tm tm = hora_local(t);
   char buf[64];
   snprintf(buf, sizeof(buf), "%04d-%02d-%02d %02d:%02d:%02d.%03d",
            tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
            tm.tm_hour, tm.tm_min, tm.tm_sec, (int)ms.count());
   return buf;
}

// "YYYYmmdd_HHMMSS"
static std::string now_compacto()
{
   std::tm tm = hora_local(std::time(nullptr));
   char buf[32];
   snprintf(buf, sizeof(buf), "%04d%02d%02d_%02d%02d%02d",
            tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
            tm.tm_hour, tm.tm_min, tm.tm_sec);
   return buf;
}

// segundos -> "HH:MM:SS" (las horas pueden pasar de 24)
static std::string hms(double s)
{
   long n = (long)(s + 0.5);
   char buf[32];
   snprintf(buf, sizeof(buf), "%02ld:%02ld:%02ld", n / 3600, (n / 60) % 60, n % 60);
   return buf;
}

// numero con formato o "NaN"
static std::string fnum(double x, int prec)
{
   if (!std::isfinite(x)) return "NaN";
   char buf[64];
   snprintf(buf, sizeof(buf), "%.*f", prec, x);
   return buf;
}

/*==================== Parseo de argumentos ====================*/

static bool parse_double(const std::string &s, double &out)
{
   if (s.empty()) return false;
   char *end = nullptr;
   errno = 0;
   double v = strtod(s.c_str(), &end);
   if (errno != 0 || end == s.c_str() || *end != '\0' || !std::isfinite(v)) return false;
   out = v;
   return true;
}

static bool parse_long(const std::string &s, long &out)
{
   if (s.empty()) return false;
   char *end = nullptr;
   errno = 0;
   long v = strtol(s.c_str(), &end, 10);
   if (errno != 0 || end == s.c_str() || *end != '\0') return false;
   out = v;
   return true;
}

struct Opcion {
   std::string nombre;   // "--freq"
   std::string valor;
   bool        tiene_valor;
};

// Separa argv[desde..] en opciones. `flags` son las opciones sin valor.
// Acepta "--opt valor" y "--opt=valor". Devuelve false si hay error.
static bool separar_opciones(int argc, char *argv[], int desde,
                             const std::vector<std::string> &flags,
                             const std::vector<std::string> &con_valor,
                             std::vector<Opcion> &out)
{
   for (int i = desde; i < argc; i++) {
      std::string a = argv[i];
      if (a == "-h") a = "--help";
      if (a.size() < 3 || a[0] != '-' || a[1] != '-') {
         fprintf(stderr, "ERROR: argumento inesperado '%s' (las opciones empiezan con --)\n", a.c_str());
         return false;
      }
      Opcion o;
      size_t eq = a.find('=');
      if (eq != std::string::npos) {
         o.nombre = a.substr(0, eq);
         o.valor = a.substr(eq + 1);
         o.tiene_valor = true;
      } else {
         o.nombre = a;
         o.tiene_valor = false;
      }
      bool es_flag = std::find(flags.begin(), flags.end(), o.nombre) != flags.end();
      bool es_val  = std::find(con_valor.begin(), con_valor.end(), o.nombre) != con_valor.end();
      if (!es_flag && !es_val) {
         fprintf(stderr, "ERROR: opcion desconocida '%s' (proba con --help)\n", o.nombre.c_str());
         return false;
      }
      if (es_flag && o.tiene_valor) {
         fprintf(stderr, "ERROR: la opcion %s no lleva valor\n", o.nombre.c_str());
         return false;
      }
      if (es_val && !o.tiene_valor) {
         if (i + 1 >= argc) {
            fprintf(stderr, "ERROR: falta el valor de la opcion %s\n", o.nombre.c_str());
            return false;
         }
         o.valor = argv[++i];   // se acepta un valor negativo, p.ej. --thr -50
         o.tiene_valor = true;
      }
      out.push_back(o);
   }
   return true;
}

/*==================== Ayuda ====================*/

static void ayuda_general()
{
   printf(
"%s\n"
"Medicion de tiempo de vuelo (TOF) de muones con la DRS4 Evaluation Board V5.\n"
"\n"
"Uso:\n"
"  drs_tof info  [--freq GSPS]          Muestra datos de la placa y de la calibracion guardada.\n"
"  drs_tof calib [--freq 5] [...]       Calibracion de tension + temporal (se graba en la EEPROM).\n"
"  drs_tof medir --dist CM [...]        Adquisicion de eventos.\n"
"\n"
"Ayuda de cada subcomando:  drs_tof medir --help   |   drs_tof calib --help\n"
"Normalmente hay que correrlo con sudo (acceso USB) salvo que esten instaladas las reglas udev.\n",
   kVersion);
}

static void ayuda_info()
{
   printf(
"Uso: drs_tof info [--freq GSPS]\n"
"\n"
"Muestra numero de serie, firmware, tipo de placa, temperatura, la frecuencia\n"
"para la que esta guardada la calibracion temporal en la EEPROM y la\n"
"frecuencia real de muestreo que surge de esa calibracion.\n"
"Con --freq, ademas verifica si la calibracion temporal es valida a esa frecuencia.\n");
}

static void ayuda_calib()
{
   printf(
"Uso: drs_tof calib [--freq 5] [--si] [--solo-tension | --solo-tiempo]\n"
"\n"
"Hace la calibracion de TENSION (offset y ganancia de cada celda) y luego la\n"
"calibracion TEMPORAL (ancho de cada celda) a la frecuencia de muestreo --freq.\n"
"La libreria graba ambas automaticamente en la EEPROM de la placa.\n"
"\n"
"IMPORTANTE: la calibracion temporal solo vale para la frecuencia a la que se hizo.\n"
"Usa la MISMA --freq que vas a usar en 'drs_tof medir'.\n"
"\n"
"Opciones:\n"
"  --freq GSPS       frecuencia de muestreo (default 5 -> efectiva 5.12 GSPS)\n"
"  --si              no pedir confirmacion (para scripts)\n"
"  --solo-tension    hacer solo la calibracion de tension\n"
"  --solo-tiempo     hacer solo la calibracion temporal (requiere tension ya calibrada)\n");
}

static void ayuda_medir()
{
   printf(
"Uso: drs_tof medir --dist CM [opciones]\n"
"\n"
"Opciones (entre corchetes el valor por defecto):\n"
"  --dist CM          separacion entre barras superior e inferior en cm (OBLIGATORIO)\n"
"  --freq GSPS        frecuencia de muestreo [5] (efectiva 5.12 GSPS, ventana ~200 ns)\n"
"  --delay NS         demora del trigger en ns [80]; mas demora = el pulso aparece\n"
"                     mas temprano en la ventana. Satura en 255 x 6.2 ns = 1581 ns\n"
"  --thr MV           umbral en mV, negativo (pulsos negativos) [-50]. Se usa para el\n"
"                     trigger por hardware y para decidir si un canal tiene pulso\n"
"  --trigger MODO     and | or | ch1 | ch2 | ch3   [and]\n"
"                     and = coincidencia (AND por hardware) de todos los --canales\n"
"                     or  = cualquiera de los --canales\n"
"  --canales LISTA    canales a leer, p.ej. 1,2,3 [1,2,3]  (CH1 arriba, CH2 cruzada, CH3 abajo)\n"
"  --fraccion F       fraccion del CFD, entre 0 y 1 [0.5]\n"
"  --nbase N          minimo de muestras para la linea de base [30]. La base es la\n"
"                     mediana de todo lo anterior al pulso (hasta 25 muestras antes del minimo)\n"
"  --nota \"texto\"     comentario libre que va al encabezado de los archivos\n"
"  --out DIR          directorio de salida [./medidas/tof/]\n"
"  --nmax N           cortar despues de N eventos [sin limite]\n"
"  --rotar-ev N       abrir archivos nuevos cada N eventos [1000]\n"
"  --rotar-seg S      ... o cada S segundos (se chequea al llegar un evento) [21600 = 6 h]\n"
"  --sin-ondas        no guardar las formas de onda (solo el CSV de eventos)\n"
"  --forzar           medir aunque la calibracion (temporal o de tension) no sea valida\n"
"\n"
"Ejemplo:\n"
"  sudo ./drs_tof medir --dist 100 --nota \"barras cruzadas, CH1 arriba\"\n"
"\n"
"Ctrl-C termina la medicion cerrando los archivos prolijamente.\n");
}

/*==================== Barra de progreso para calibraciones ====================*/

class BarraProgreso : public DRSCallback
{
public:
   BarraProgreso() : fUltimo(-1) {}
   void Progress(int prog)
   {
      if (prog == fUltimo) return;
      fUltimo = prog;
      int n = prog / 2;
      if (n < 0) n = 0;
      if (n > 50) n = 50;
      printf("\r  [");
      for (int i = 0; i < 50; i++) putchar(i < n ? '=' : '-');
      printf("] %3d%%", prog);
      fflush(stdout);
   }
   void Terminar() { printf("\n"); fflush(stdout); }
private:
   int fUltimo;
};

/*==================== Apertura de la placa ====================*/

struct InfoPlaca {
   int    serie = 0;
   int    firmware = 0;
   int    tipo = 0;
   int    drs_tipo = 0;
   double temperatura = 0;
};

// Abre la primera placa. Devuelve nullptr si no hay.
static DRSBoard *abrir_placa(DRS *&drs, InfoPlaca &info)
{
   drs = new DRS();
   char err[256];
   if (drs->GetError(err, sizeof(err)))
      fprintf(stderr, "Aviso de la libreria DRS: %s\n", err);

   int n = drs->GetNumberOfBoards();
   if (n == 0) {
      fprintf(stderr,
         "ERROR: no se encontro ninguna placa DRS4.\n"
         "  - Esta enchufado el USB?\n"
         "  - Corriste con sudo (o estan instaladas las reglas udev 41-drs.rules)?\n");
      return nullptr;
   }
   for (int i = 0; i < n; i++) {
      DRSBoard *bb = drs->GetBoard(i);
      printf("Placa DRS4 encontrada: serie #%d, firmware %d, tipo %d\n",
             bb->GetBoardSerialNumber(), bb->GetFirmwareVersion(), bb->GetBoardType());
   }
   if (n > 1)
      printf("Hay %d placas; se usa la primera.\n", n);

   DRSBoard *b = drs->GetBoard(0);
   b->Init();   // lee la calibracion desde la EEPROM

   info.serie       = b->GetBoardSerialNumber();
   info.firmware    = b->GetFirmwareVersion();
   info.tipo        = b->GetBoardType();
   info.drs_tipo    = b->GetDRSType();
   info.temperatura = b->GetTemperature();
   return b;
}

static double lut_delay_ns(int firmware)
{
   return firmware >= 17147 ? kLutDelayNuevo : kLutDelayViejo;
}

// Frecuencia que efectivamente programa SetFrequency (ver DRS.cpp:
// ticks = round(1.024/f * 60 MHz), f_ef = 1.024/ticks * 60) -> 5 GSPS da 5.12 GSPS.
static double frecuencia_efectiva(double f)
{
   const double refclk = 60.0;   // placas tipo 5,7,8,9
   int ticks = (int)(1.024 / f * refclk + 0.5);
   if (ticks <= 0) return f;
   return 1.024 / ticks * refclk;
}

/*==================== Subcomando: info ====================*/

static int cmd_info(int argc, char *argv[])
{
   std::vector<Opcion> ops;
   if (!separar_opciones(argc, argv, 2, {"--help"}, {"--freq"}, ops)) return 1;
   double freq = -1;
   for (auto &o : ops) {
      if (o.nombre == "--help") { ayuda_info(); return 0; }
      if (o.nombre == "--freq") {
         if (!parse_double(o.valor, freq) || freq < 0.1 || freq > 6) {
            fprintf(stderr, "ERROR: --freq invalida (rango 0.1 .. 6 GSPS)\n");
            return 1;
         }
      }
   }

   DRS *drs = nullptr;
   InfoPlaca info;
   DRSBoard *b = abrir_placa(drs, info);
   if (!b) { delete drs; return 1; }

   // Despues de Init() la frecuencia nominal queda en la frecuencia calibrada
   // (si existe), asi que GetTrueFrequency() da la frecuencia real calibrada.
   double fcal = b->GetCalibratedFrequency();

   printf("==========================================\n");
   printf("Numero de serie:          %d\n", info.serie);
   printf("Firmware:                 %d\n", info.firmware);
   printf("Tipo de placa:            %d %s\n", info.tipo, info.tipo == 9 ? "(Evaluation Board V5)" : "");
   printf("Tipo de chip:             DRS%d\n", info.drs_tipo);
   printf("Temperatura:              %.1f C\n", info.temperatura);
   printf("Calib. de tension:        %s (rango calibrado %.2f V ... %.2f V)\n",
          b->IsVoltageCalibrationValid() ? "VALIDA" : "NO VALIDA / inexistente",
          b->GetCalibratedInputRange() - 0.5, b->GetCalibratedInputRange() + 0.5);
   if (fcal > 0)
      printf("Calib. temporal (EEPROM): hecha a %.4f GSPS\n", fcal);
   else
      printf("Calib. temporal (EEPROM): NO hay calibracion temporal guardada (valor %.3f)\n", fcal);
   printf("Frecuencia nominal actual:%.4f GSPS\n", b->GetNominalFrequency());
   printf("Frecuencia real:          %.4f GSPS%s\n", b->GetTrueFrequency(),
          b->IsTimingCalibrationValid() ? " (segun la calibracion temporal)" : " (sin calibracion: = nominal)");
   printf("Demora de trigger maxima: %d x %.1f ns = %.0f ns\n", kMaxTicks,
          lut_delay_ns(info.firmware), kMaxTicks * lut_delay_ns(info.firmware));

   if (freq > 0) {
      if (!b->SetFrequency(freq, true))
         fprintf(stderr, "AVISO: SetFrequency(%.3f) fallo (no engancho el PLL?)\n", freq);
      printf("------------------------------------------\n");
      printf("Pedido --freq %.3f -> frecuencia nominal efectiva %.4f GSPS\n", freq, b->GetNominalFrequency());
      printf("Calibracion temporal valida a esa frecuencia: %s\n",
             b->IsTimingCalibrationValid() ? "SI" : "NO -> correr 'drs_tof calib --freq ...'");
   }
   printf("==========================================\n");

   delete drs;
   return 0;
}

/*==================== Subcomando: calib ====================*/

static int cmd_calib(int argc, char *argv[])
{
   std::vector<Opcion> ops;
   if (!separar_opciones(argc, argv, 2, {"--help", "--si", "--solo-tension", "--solo-tiempo"},
                         {"--freq"}, ops))
      return 1;
   double freq = 5.0;
   bool si = false, solo_v = false, solo_t = false;
   for (auto &o : ops) {
      if (o.nombre == "--help") { ayuda_calib(); return 0; }
      else if (o.nombre == "--si") si = true;
      else if (o.nombre == "--solo-tension") solo_v = true;
      else if (o.nombre == "--solo-tiempo") solo_t = true;
      else if (o.nombre == "--freq") {
         if (!parse_double(o.valor, freq) || freq < 0.5 || freq > 5.2) {
            fprintf(stderr, "ERROR: --freq invalida (rango 0.5 .. 5.12 GSPS)\n");
            return 1;
         }
      }
   }
   if (solo_v && solo_t) {
      fprintf(stderr, "ERROR: --solo-tension y --solo-tiempo son excluyentes\n");
      return 1;
   }
   bool hacer_v = !solo_t, hacer_t = !solo_v;

   printf("\n");
   printf("================= CALIBRACION DE LA DRS4 =================\n");
   printf(" Frecuencia pedida: %.3f GSPS (efectiva %.4f GSPS)\n", freq, frecuencia_efectiva(freq));
   printf(" Se va a hacer:     %s%s%s\n", hacer_v ? "calibracion de TENSION" : "",
          (hacer_v && hacer_t) ? " + " : "", hacer_t ? "calibracion TEMPORAL" : "");
   printf("\n");
   printf(" ANTES DE SEGUIR:\n");
   printf("  1) APAGA la alta tension de los PMTs.\n");
   printf("  2) DESCONECTA los cables de las 4 entradas (CH1..CH4) y del EXT TRIG.\n");
   printf("     No tiene que llegar NINGUNA senal a las entradas: la placa conecta\n");
   printf("     internamente sus tensiones de referencia y un reloj de 100 MHz\n");
   printf("     a los canales, y cualquier senal externa arruina la calibracion.\n");
   printf("  3) Ideal: placa prendida hace al menos 10-15 minutos (temperatura estable).\n");
   printf("     Conviene recalibrar si la temperatura cambia mucho (> 5 C).\n");
   printf("  4) Duracion aproximada: tension ~30 s, temporal ~1-3 min en la Raspberry.\n");
   printf("     NO desenchufes el USB mientras corre: se escribe la EEPROM.\n");
   printf("\n");
   printf(" La calibracion temporal SOLO vale para la frecuencia a la que se hace:\n");
   printf(" medi despues con la misma --freq (%.3f).\n", freq);
   printf("==========================================================\n");

   if (!si) {
      printf("Presiona ENTER para empezar (Ctrl-C para cancelar)... ");
      fflush(stdout);
      char line[64];
      if (!fgets(line, sizeof(line), stdin)) {
         fprintf(stderr, "\nNo se pudo leer la confirmacion; cancelado (usa --si para no preguntar).\n");
         return 1;
      }
   }

   DRS *drs = nullptr;
   InfoPlaca info;
   DRSBoard *b = abrir_placa(drs, info);
   if (!b) { delete drs; return 1; }

   if (info.drs_tipo != 4) {
      fprintf(stderr, "ERROR: este programa es solo para chips DRS4\n");
      delete drs;
      return 1;
   }
   printf("Temperatura de la placa antes de calibrar: %.1f C\n", info.temperatura);

   int ok_t = 1;

   if (hacer_v) {
      // Igual que el comando "calib" de drscl (USB, modo 1024 celdas, rango 0 => -0.5..0.5 V)
      printf("\n[1] Calibracion de TENSION a %.3f GSPS, rango -0.5 .. 0.5 V\n", freq);
      BarraProgreso p;
      b->SetChannelConfig(0, 8, 8);
      b->SetRefclk(0);
      if (!b->SetFrequency(freq, true))
         fprintf(stderr, "AVISO: SetFrequency(%.3f) fallo (no engancho el PLL?)\n", freq);
      b->SetInputRange(0);
      b->CalibrateVolt(&p);   // graba sola en la EEPROM (paginas 0, 1 y 2)
      p.Terminar();
      printf("    Calibracion de tension terminada y grabada en la EEPROM.\n");
   }

   if (hacer_t) {
      // Igual que el comando "tcalib" de drscl
      if (info.firmware < 13279) {
         fprintf(stderr, "ERROR: la calibracion temporal requiere firmware >= 13279 (este: %d)\n", info.firmware);
         delete drs;
         return 1;
      }
      if (!b->IsVoltageCalibrationValid())
         fprintf(stderr, "AVISO: no hay calibracion de tension valida; la temporal la usa. "
                         "Conviene correr sin --solo-tiempo.\n");
      printf("\n[2] Calibracion TEMPORAL a %.3f GSPS (reloj interno de 100 MHz)\n", freq);
      BarraProgreso p;
      if (!b->SetFrequency(freq, true))
         fprintf(stderr, "AVISO: SetFrequency(%.3f) fallo (no engancho el PLL?)\n", freq);
      ok_t = b->CalibrateTiming(&p);   // graba sola en la EEPROM (paginas 0 y 2)
      p.Terminar();
      if (!ok_t) {
         fprintf(stderr,
            "ERROR: la calibracion temporal reporto errores (\"please check waveforms\").\n"
            "  OJO: la libreria igual escribio la EEPROM con ese resultado. Verifica que no\n"
            "  haya nada conectado a las entradas y REPETI 'drs_tof calib --solo-tiempo'.\n");
      } else
         printf("    Calibracion temporal terminada y grabada en la EEPROM.\n");
   }

   // Verificacion: se re-lee la calibracion desde la EEPROM (Init() llama a ReadCalibration())
   printf("\n[3] Verificacion (re-leyendo la EEPROM)\n");
   b->Init();
   if (!b->SetFrequency(freq, true))
      fprintf(stderr, "AVISO: SetFrequency(%.3f) fallo (no engancho el PLL?)\n", freq);
   bool vv = b->IsVoltageCalibrationValid();
   bool tv = b->IsTimingCalibrationValid();
   printf("    Temperatura:                  %.1f C\n", b->GetTemperature());
   printf("    Calibracion de tension:       %s (rango %.2f .. %.2f V)\n", vv ? "VALIDA" : "NO VALIDA",
          b->GetCalibratedInputRange() - 0.5, b->GetCalibratedInputRange() + 0.5);
   printf("    Frecuencia calibrada (EEPROM): %.4f GSPS\n", b->GetCalibratedFrequency());
   printf("    Frecuencia nominal actual:     %.4f GSPS\n", b->GetNominalFrequency());
   printf("    IsTimingCalibrationValid():    %s\n", tv ? "SI" : "NO");
   printf("    Frecuencia real (calibrada):   %.4f GSPS  -> ventana de %.2f ns\n",
          b->GetTrueFrequency(), kNBins / b->GetTrueFrequency());

   bool todo_ok = (!hacer_v || vv) && (!hacer_t || (tv && ok_t));
   if (todo_ok)
      printf("\nListo. Ya podes reconectar los cables y prender la HV.\n"
             "Medi con:  sudo ./drs_tof medir --freq %g --dist <cm>\n", freq);
   else
      printf("\nATENCION: la verificacion fallo. Revisa los mensajes de arriba y repeti la calibracion.\n");

   delete drs;
   return todo_ok ? 0 : 1;
}

/*==================== Analisis de un canal ====================*/

struct ResultadoCanal {
   bool   leido = false;   // canal habilitado
   bool   pulso = false;   // se encontro un pulso por debajo del umbral
   double base  = NAN;     // linea de base [mV]
   double amp   = NAN;     // minimo - base [mV] (negativo)
   double t_le  = NAN;     // cruce del umbral (leading edge) [ns]
   double t_cfd = NAN;     // cruce de fraccion*amp [ns]
};

// Busca hacia atras desde imin el cruce descendente del nivel `nivel`
// e interpola linealmente con el eje temporal calibrado.
static double cruce_hacia_atras(const float *t, const float *u, int imin, int istart, double nivel)
{
   for (int k = imin - 1; k >= istart; k--) {
      if (u[k] > nivel) {
         // u[k] > nivel >= u[k+1]
         double du = (double)u[k + 1] - (double)u[k];
         if (du == 0) return t[k];
         return t[k] + (nivel - u[k]) * ((double)t[k + 1] - (double)t[k]) / du;
      }
   }
   return NAN;
}

static ResultadoCanal analizar_canal(const float *t, const float *u, int n,
                                     double thr_mV, double fraccion, int nskip, int nbase)
{
   ResultadoCanal r;
   r.leido = true;

   // 1) minimo (no depende de la linea de base), salteando la region inicial
   //    reservada a la linea de base y las ultimas celdas
   int i0 = nskip + nbase;
   int i1 = n - 3;
   int imin = i0;
   for (int i = i0; i < i1; i++)
      if (u[i] < u[imin]) imin = i;

   // 2) linea de base: mediana de TODAS las muestras anteriores al pulso
   //    (desde nskip hasta kMargen muestras antes del minimo, y como minimo
   //    nbase muestras). La mediana sobre un tramo largo es robusta frente al
   //    ringing/ruido que suele haber al principio de la ventana.
   const int kMargen = 25;   // ~5 ns a 5 GSPS
   int fin_base = std::max(nskip + nbase, imin - kMargen);
   std::vector<float> tmp(u + nskip, u + fin_base);
   size_t mid = tmp.size() / 2;
   std::nth_element(tmp.begin(), tmp.begin() + mid, tmp.end());
   r.base = tmp[mid];
   r.amp  = u[imin] - r.base;

   if (r.amp > thr_mV)   // thr negativo: el pulso no llega al umbral
      return r;

   // 3) anti-pico: al menos 3 muestras contiguas por debajo del umbral alrededor del minimo
   double nivel_le = r.base + thr_mV;
   int nbajo = 0;
   for (int k = imin; k >= 0 && u[k] <= nivel_le; k--) nbajo++;
   for (int k = imin + 1; k < n && u[k] <= nivel_le; k++) nbajo++;
   if (nbajo < 3)
      return r;

   r.pulso = true;
   r.t_le  = cruce_hacia_atras(t, u, imin, nskip, nivel_le);
   r.t_cfd = cruce_hacia_atras(t, u, imin, nskip, r.base + fraccion * r.amp);
   return r;
}

/*==================== Configuracion de la medicion ====================*/

struct ConfigMedicion {
   double dist_cm   = NAN;
   double freq      = 5.0;
   int    delay_ns  = 80;
   double thr_mV    = -50.0;
   std::string trigger = "and";
   std::vector<int> canales {1, 2, 3};
   double fraccion  = 0.5;
   int    nbase     = 30;
   int    nskip     = 3;
   std::string nota;
   std::string out  = "./medidas/tof/";
   long   nmax      = 0;      // 0 = sin limite
   long   rotar_ev  = 1000;
   long   rotar_seg = 21600;
   bool   sin_ondas = false;
   bool   forzar    = false;
   std::string comando;      // linea de comando completa
};

struct EstadoPlaca {
   InfoPlaca info;
   double f_nominal = 0, f_calibrada = 0, f_real = 0, ventana_ns = 0;
   bool   t_valida = false, v_valida = false;
   double delay_efectivo_ns = 0;
   double pos_trigger_ns = 0;
   int    mascara = 0;
};

static bool tiene_canal(const ConfigMedicion &c, int ch)
{
   return std::find(c.canales.begin(), c.canales.end(), ch) != c.canales.end();
}

static std::string lista_canales(const std::vector<int> &v)
{
   std::string s;
   for (size_t i = 0; i < v.size(); i++) {
      if (i) s += ",";
      s += std::to_string(v[i]);
   }
   return s;
}

static int mascara_trigger(const ConfigMedicion &c)
{
   // DRS.cpp SetTriggerSource (placas 8/9):
   // OR  Bit0=CH1, Bit1=CH2, Bit2=CH3, Bit3=CH4, Bit4=EXT
   // AND Bit8=CH1, Bit9=CH2, Bit10=CH3, Bit11=CH4, Bit12=EXT
   int m = 0;
   if (c.trigger == "and") {
      for (int ch : c.canales) m |= 1 << (8 + ch - 1);
   } else if (c.trigger == "or") {
      for (int ch : c.canales) m |= 1 << (ch - 1);
   } else {
      int ch = c.trigger[2] - '0';
      m = 1 << (ch - 1);
   }
   return m;
}

static bool parsear_config_medir(int argc, char *argv[], ConfigMedicion &c, bool &pidio_ayuda)
{
   pidio_ayuda = false;
   std::vector<Opcion> ops;
   if (!separar_opciones(argc, argv, 2,
         {"--help", "--sin-ondas", "--forzar"},
         {"--dist", "--freq", "--delay", "--thr", "--trigger", "--canales", "--fraccion",
          "--nbase", "--nota", "--out", "--nmax", "--rotar-ev", "--rotar-seg"}, ops))
      return false;

   for (int i = 0; i < argc; i++) {
      if (i) c.comando += " ";
      std::string a = argv[i];
      if (a.find(' ') != std::string::npos) c.comando += "\"" + a + "\"";
      else c.comando += a;
   }

   bool ok = true;
   for (auto &o : ops) {
      const std::string &n = o.nombre;
      const std::string &v = o.valor;
      double d; long l;
      if (n == "--help") { pidio_ayuda = true; return true; }
      else if (n == "--sin-ondas") c.sin_ondas = true;
      else if (n == "--forzar")    c.forzar = true;
      else if (n == "--dist") {
         if (!parse_double(v, d) || d < 0 || d > 10000) {
            fprintf(stderr, "ERROR: --dist debe ser un numero >= 0 en cm (recibido '%s')\n", v.c_str()); ok = false;
         } else c.dist_cm = d;
      } else if (n == "--freq") {
         if (!parse_double(v, d) || d < 0.5 || d > 5.2) {
            fprintf(stderr, "ERROR: --freq debe estar entre 0.5 y 5.12 GSPS (recibido '%s')\n", v.c_str()); ok = false;
         } else c.freq = d;
      } else if (n == "--delay") {
         if (!parse_long(v, l) || l < 0 || l > 100000) {
            fprintf(stderr, "ERROR: --delay debe ser un entero >= 0 en ns (recibido '%s')\n", v.c_str()); ok = false;
         } else c.delay_ns = (int)l;
      } else if (n == "--thr") {
         if (!parse_double(v, d) || d >= 0 || d < -500) {
            fprintf(stderr, "ERROR: --thr debe ser negativo, entre -500 y 0 mV (recibido '%s')\n", v.c_str()); ok = false;
         } else c.thr_mV = d;
      } else if (n == "--trigger") {
         std::string m = v;
         for (auto &ch : m) ch = (char)tolower((unsigned char)ch);
         if (m != "and" && m != "or" && m != "ch1" && m != "ch2" && m != "ch3") {
            fprintf(stderr, "ERROR: --trigger debe ser and, or, ch1, ch2 o ch3 (recibido '%s')\n", v.c_str()); ok = false;
         } else c.trigger = m;
      } else if (n == "--canales") {
         std::vector<int> lista;
         std::stringstream ss(v);
         std::string item;
         bool okc = true;
         while (std::getline(ss, item, ',')) {
            long ch;
            if (!parse_long(item, ch) || ch < 1 || ch > 3) { okc = false; break; }
            if (std::find(lista.begin(), lista.end(), (int)ch) == lista.end()) lista.push_back((int)ch);
         }
         if (!okc || lista.empty()) {
            fprintf(stderr, "ERROR: --canales debe ser una lista de 1, 2 y/o 3 separada por comas (recibido '%s')\n", v.c_str());
            ok = false;
         } else {
            std::sort(lista.begin(), lista.end());
            c.canales = lista;
         }
      } else if (n == "--fraccion") {
         if (!parse_double(v, d) || d <= 0 || d >= 1) {
            fprintf(stderr, "ERROR: --fraccion debe estar entre 0 y 1 (recibido '%s')\n", v.c_str()); ok = false;
         } else c.fraccion = d;
      } else if (n == "--nbase") {
         if (!parse_long(v, l) || l < 5 || l > 500) {
            fprintf(stderr, "ERROR: --nbase debe ser un entero entre 5 y 500 (recibido '%s')\n", v.c_str()); ok = false;
         } else c.nbase = (int)l;
      } else if (n == "--nota") {
         std::string s = v;
         for (auto &ch : s) if (ch == '\n' || ch == '\r') ch = ' ';
         c.nota = s;
      } else if (n == "--out") {
         if (v.empty()) { fprintf(stderr, "ERROR: --out vacio\n"); ok = false; }
         else c.out = v;
      } else if (n == "--nmax") {
         if (!parse_long(v, l) || l < 1) {
            fprintf(stderr, "ERROR: --nmax debe ser un entero >= 1 (recibido '%s')\n", v.c_str()); ok = false;
         } else c.nmax = l;
      } else if (n == "--rotar-ev") {
         if (!parse_long(v, l) || l < 1) {
            fprintf(stderr, "ERROR: --rotar-ev debe ser un entero >= 1 (recibido '%s')\n", v.c_str()); ok = false;
         } else c.rotar_ev = l;
      } else if (n == "--rotar-seg") {
         if (!parse_long(v, l) || l < 10) {
            fprintf(stderr, "ERROR: --rotar-seg debe ser un entero >= 10 (recibido '%s')\n", v.c_str()); ok = false;
         } else c.rotar_seg = l;
      }
   }

   if (!std::isfinite(c.dist_cm)) {
      fprintf(stderr, "ERROR: falta --dist <cm> (separacion entre las barras, va al encabezado)\n");
      ok = false;
   }
   if (ok && c.trigger.size() == 3 && c.trigger[0] == 'c') {
      int ch = c.trigger[2] - '0';
      if (!tiene_canal(c, ch)) {
         fprintf(stderr, "ERROR: --trigger %s pero el canal %d no esta en --canales\n", c.trigger.c_str(), ch);
         ok = false;
      }
   }
   if (c.nskip + c.nbase >= kNBins / 2) {
      fprintf(stderr, "ERROR: --nbase demasiado grande\n");
      ok = false;
   }
   if (!c.out.empty() && c.out.back() != '/' && c.out.back() != '\\')
      c.out += '/';
   return ok;
}

/*==================== Escritura con rotacion de archivos ====================*/

struct EscritoresRotativos {
   FILE *fev   = nullptr;   // <run>_idxN_eventos.csv
   FILE *fondas = nullptr;  // <run>_idxN_ondas.txt
   std::string base;        // directorio + run_id
   std::string run_id;
   std::string archivo_ev_actual;
   int  file_index = 0;
   long events_in_file = 0;
   long max_ev = 1000;
   long max_seg = 21600;
   bool con_ondas = true;
   std::chrono::steady_clock::time_point opened_at;

   void cerrar()
   {
      if (fev)    { fflush(fev);    fclose(fev);    fev = nullptr; }
      if (fondas) { fflush(fondas); fclose(fondas); fondas = nullptr; }
   }

   bool hay_que_rotar() const
   {
      using namespace std::chrono;
      if (events_in_file <= 0) return false;   // no dejar archivos vacios
      if (events_in_file >= max_ev) return true;
      auto secs = duration_cast<seconds>(steady_clock::now() - opened_at).count();
      return secs >= max_seg;
   }

   bool abrir(const ConfigMedicion &c, const EstadoPlaca &e);
};

static void escribir_encabezado(FILE *f, const ConfigMedicion &c, const EstadoPlaca &e,
                                const std::string &run_id, int idx, bool es_csv)
{
   fprintf(f, "# programa: %s\n", kVersion);
   fprintf(f, "# comando: %s\n", c.comando.c_str());
   fprintf(f, "# fecha_apertura: %s\n", now_timestamp().c_str());
   fprintf(f, "# run_id: %s\n", run_id.c_str());
   fprintf(f, "# archivo_idx: %d\n", idx);
   fprintf(f, "# placa_serie: %d\n", e.info.serie);
   fprintf(f, "# placa_firmware: %d\n", e.info.firmware);
   fprintf(f, "# placa_tipo: %d\n", e.info.tipo);
   fprintf(f, "# temperatura_C: %.1f\n", e.info.temperatura);
   fprintf(f, "# freq_pedida_GSPS: %.4f\n", c.freq);
   fprintf(f, "# freq_nominal_GSPS: %.4f\n", e.f_nominal);
   fprintf(f, "# freq_calibrada_GSPS: %.4f\n", e.f_calibrada);
   fprintf(f, "# freq_real_GSPS: %.4f\n", e.f_real);
   fprintf(f, "# ventana_ns: %.3f\n", e.ventana_ns);
   fprintf(f, "# calib_temporal_valida: %s\n", e.t_valida ? "si" : "NO");
   fprintf(f, "# calib_tension_valida: %s\n", e.v_valida ? "si" : "NO");
   fprintf(f, "# delay_pedido_ns: %d\n", c.delay_ns);
   fprintf(f, "# delay_efectivo_ns: %.1f\n", e.delay_efectivo_ns);
   fprintf(f, "# pos_trigger_estimada_ns: %.1f\n", e.pos_trigger_ns);
   fprintf(f, "# thr_mV: %.1f\n", c.thr_mV);
   fprintf(f, "# polaridad_trigger: negativa (flanco descendente)\n");
   fprintf(f, "# trigger: %s\n", c.trigger.c_str());
   fprintf(f, "# trigger_mascara: 0x%04X\n", e.mascara);
   fprintf(f, "# canales: %s\n", lista_canales(c.canales).c_str());
   fprintf(f, "# dist_cm: %g\n", c.dist_cm);
   fprintf(f, "# fraccion_cfd: %.3f\n", c.fraccion);
   fprintf(f, "# nbase: %d\n", c.nbase);
   fprintf(f, "# nskip: %d\n", c.nskip);
   fprintf(f, "# nota: %s\n", c.nota.c_str());
   if (es_csv) {
      fprintf(f, "# Tiempos en ns desde el inicio de la ventana (eje calibrado de cada canal),\n");
      fprintf(f, "# amplitudes y lineas de base en mV (amp = minimo - base, negativa).\n");
      fprintf(f, "# t_cfd: cruce de fraccion_cfd*amp; t_le: cruce de base+thr. dtij = t_cfd_i - t_cfd_j.\n");
      fprintf(f, "# NaN = canal no leido; los tiempos (y los dt) tambien son NaN si el canal no tiene\n");
      fprintf(f, "# pulso (amp > thr o menos de 3 muestras bajo el umbral). amp y base se informan igual.\n");
      fprintf(f, "evento,timestamp,t_cfd1_ns,t_cfd2_ns,t_cfd3_ns,t_le1_ns,t_le2_ns,t_le3_ns,"
                 "amp1_mV,amp2_mV,amp3_mV,base1_mV,base2_mV,base3_mV,dt31_ns,dt21_ns,dt32_ns\n");
   } else {
      fprintf(f, "# Columns:");
      for (int ch : c.canales) fprintf(f, " t%d[ns] u%d[mV]", ch, ch);
      fprintf(f, "\n");
   }
   fflush(f);
}

bool EscritoresRotativos::abrir(const ConfigMedicion &c, const EstadoPlaca &e)
{
   cerrar();
   std::ostringstream nev, non;
   nev << base << "_idx" << file_index << "_eventos.csv";
   non << base << "_idx" << file_index << "_ondas.txt";

   fev = fopen(nev.str().c_str(), "w");
   if (!fev) { perror(("ERROR: no se pudo abrir " + nev.str()).c_str()); return false; }
   escribir_encabezado(fev, c, e, run_id, file_index, true);
   archivo_ev_actual = nev.str();

   if (con_ondas) {
      fondas = fopen(non.str().c_str(), "w");
      if (!fondas) {
         perror(("ERROR: no se pudo abrir " + non.str()).c_str());
         fclose(fev); fev = nullptr;
         return false;
      }
      escribir_encabezado(fondas, c, e, run_id, file_index, false);
   }

   printf("Archivo nuevo: %s%s\n", nev.str().c_str(), con_ondas ? " (+ _ondas.txt)" : "");
   events_in_file = 0;
   opened_at = std::chrono::steady_clock::now();
   file_index++;
   return true;
}

/*==================== Subcomando: medir ====================*/

static int cmd_medir(int argc, char *argv[])
{
   ConfigMedicion c;
   bool pidio_ayuda;
   if (!parsear_config_medir(argc, argv, c, pidio_ayuda)) {
      fprintf(stderr, "(ver 'drs_tof medir --help')\n");
      return 1;
   }
   if (pidio_ayuda) { ayuda_medir(); return 0; }

   if (!tiene_canal(c, 1) || !tiene_canal(c, 3))
      printf("AVISO: sin CH1 y CH3 no se puede calcular dt31 (TOF).\n");

   /* ---- placa ---- */
   DRS *drs = nullptr;
   EstadoPlaca e;
   DRSBoard *b = abrir_placa(drs, e.info);
   if (!b) { delete drs; return 1; }

   if (e.info.drs_tipo != 4 || (e.info.tipo != 8 && e.info.tipo != 9)) {
      fprintf(stderr, "ERROR: se esperaba una DRS4 Evaluation Board V4/V5 (tipo 8 o 9); esta es tipo %d\n",
              e.info.tipo);
      delete drs;
      return 1;
   }

   if (!b->SetFrequency(c.freq, true)) {
      fprintf(stderr, "ERROR: SetFrequency(%.3f) fallo (no engancho el PLL)\n", c.freq);
      delete drs;
      return 1;
   }
   b->SetTranspMode(1);
   b->SetInputRange(0);     // -0.5 V ... +0.5 V
   b->EnableAcal(0, 0);
   b->EnableTcal(0);        // sin reloj de calibracion: datos reales

   e.f_nominal   = b->GetNominalFrequency();
   e.f_calibrada = b->GetCalibratedFrequency();
   e.t_valida    = b->IsTimingCalibrationValid();
   e.v_valida    = b->IsVoltageCalibrationValid();
   e.f_real      = b->GetTrueFrequency();
   e.ventana_ns  = kNBins / e.f_real;

   /* ---- chequeo de calibraciones ---- */
   if (!e.t_valida) {
      fprintf(stderr, "\n*** La calibracion temporal NO es valida a esta frecuencia ***\n");
      fprintf(stderr, "    Frecuencia nominal pedida: %.4f GSPS\n", e.f_nominal);
      if (e.f_calibrada > 0)
         fprintf(stderr, "    La placa esta calibrada a: %.4f GSPS\n", e.f_calibrada);
      else
         fprintf(stderr, "    La placa NO tiene calibracion temporal guardada.\n");
      fprintf(stderr, "    Sin calibracion, GetTime() devuelve un eje uniforme nominal y los\n"
                      "    tiempos tienen errores de hasta ~cientos de ps.\n"
                      "    Solucion: 'sudo ./drs_tof calib --freq %g' (o medir con --freq %.4g).\n",
              c.freq, e.f_calibrada > 0 ? e.f_calibrada : c.freq);
      if (!c.forzar) {
         fprintf(stderr, "    Para medir igual usa --forzar.\n");
         delete drs;
         return 1;
      }
      fprintf(stderr, "    --forzar: se mide igual (queda anotado en el encabezado).\n\n");
   }
   if (!e.v_valida) {
      fprintf(stderr, "\n*** La placa NO tiene calibracion de tension valida ***\n"
                      "    Correr 'sudo ./drs_tof calib --freq %g'.\n", c.freq);
      if (!c.forzar) {
         fprintf(stderr, "    Para medir igual usa --forzar.\n");
         delete drs;
         return 1;
      }
   }

   /* ---- trigger ---- */
   e.mascara = mascara_trigger(c);
   b->EnableTrigger(1, 0);
   b->SetTriggerSource(e.mascara);
   b->SetTriggerLevel(c.thr_mV / 1000.0);   // en volts, igual para los 4 canales
   b->SetTriggerPolarity(true);             // pulsos negativos: flanco descendente
   b->SetTriggerDelayNs(c.delay_ns);

   double lut = lut_delay_ns(e.info.firmware);
   int ticks = (int)(c.delay_ns / lut + 0.5);
   bool satura = ticks > kMaxTicks;
   if (ticks > kMaxTicks) ticks = kMaxTicks;
   e.delay_efectivo_ns = ticks * lut;
   // retardo fijo medido por PSI (DRS.cpp, SetTriggerDelayPercent): 23.5 + 28.2/f ns
   e.pos_trigger_ns = e.ventana_ns - (e.delay_efectivo_ns + 23.5 + 28.2 / e.f_nominal);

   /* ---- directorio y archivos ---- */
   if (!make_dirs(c.out)) { delete drs; return 1; }

   EscritoresRotativos W;
   {
      char dbuf[64];
      snprintf(dbuf, sizeof(dbuf), "tof_d%gcm_", c.dist_cm);
      W.run_id = std::string(dbuf) + now_compacto();
   }
   W.base      = c.out + W.run_id;
   W.max_ev    = c.rotar_ev;
   W.max_seg   = c.rotar_seg;
   W.con_ondas = !c.sin_ondas;

   /* ---- resumen de configuracion ---- */
   printf("\n==================== CONFIGURACION ====================\n");
   printf(" Placa:            serie %d, firmware %d, tipo %d, %.1f C\n",
          e.info.serie, e.info.firmware, e.info.tipo, e.info.temperatura);
   printf(" Frecuencia:       pedida %.3f, nominal %.4f, real %.4f GSPS (ventana %.1f ns)\n",
          c.freq, e.f_nominal, e.f_real, e.ventana_ns);
   printf(" Calib. temporal:  %s (EEPROM: %.4f GSPS)\n", e.t_valida ? "VALIDA" : "NO VALIDA (--forzar)",
          e.f_calibrada);
   printf(" Calib. tension:   %s\n", e.v_valida ? "VALIDA" : "NO VALIDA (--forzar)");
   printf(" Trigger:          %s, mascara 0x%04X, umbral %.1f mV, flanco descendente\n",
          c.trigger.c_str(), e.mascara, c.thr_mV);
   printf(" Delay:            pedido %d ns -> efectivo %.1f ns (%d ticks de %.1f ns)\n",
          c.delay_ns, e.delay_efectivo_ns, ticks, lut);
   printf(" Pulso de trigger: aprox. en t ~ %.0f ns de la ventana (estimacion)\n", e.pos_trigger_ns);
   printf(" Canales:          %s\n", lista_canales(c.canales).c_str());
   printf(" Distancia:        %g cm\n", c.dist_cm);
   printf(" CFD:              fraccion %.2f, base = mediana desde la muestra %d hasta 25 antes del pulso (min. %d)\n",
          c.fraccion, c.nskip, c.nbase);
   printf(" Nota:             %s\n", c.nota.empty() ? "(ninguna)" : c.nota.c_str());
   printf(" Salida:           %s%s_idx*_eventos.csv%s\n", c.out.c_str(), W.run_id.c_str(),
          c.sin_ondas ? " (sin ondas)" : " + _ondas.txt");
   printf(" Rotacion:         cada %ld eventos o %ld s\n", c.rotar_ev, c.rotar_seg);
   printf(" Eventos maximos:  %s\n", c.nmax > 0 ? std::to_string(c.nmax).c_str() : "sin limite");
   printf("=======================================================\n");

   if (satura)
      printf("AVISO: --delay %d ns supera el maximo de %d x %.1f = %.0f ns; queda saturado.\n",
             c.delay_ns, kMaxTicks, lut, kMaxTicks * lut);
   if (e.pos_trigger_ns < 10)
      printf("AVISO: con este --delay el pulso que dispara queda al principio o FUERA de la\n"
             "       ventana (t ~ %.0f ns). Baja --delay (la ventana dura %.0f ns).\n",
             e.pos_trigger_ns, e.ventana_ns);
   else if (e.pos_trigger_ns > e.ventana_ns - 40)
      printf("AVISO: con este --delay el pulso queda cerca del FINAL de la ventana\n"
             "       (t ~ %.0f ns de %.0f ns). Subi --delay.\n", e.pos_trigger_ns, e.ventana_ns);

   if (!W.abrir(c, e)) { delete drs; return 1; }

   signal(SIGINT, manejador_senal);
   signal(SIGTERM, manejador_senal);

   /* ---- loop de adquisicion ---- */
   static float tiempo[4][kNBins];
   static float onda[4][kNBins];

   const bool terminal = stdout_es_terminal();
   auto t_inicio = std::chrono::steady_clock::now();
   auto t_ultimo_latido = t_inicio;
   bool linea_latido = false;

   long nev = 0;
   long n_dt31 = 0;
   double media_dt31 = 0, m2_dt31 = 0;   // Welford

   printf("\nEsperando eventos... (Ctrl-C para terminar)\n");
   fflush(stdout);

   while (!g_stop && (c.nmax <= 0 || nev < c.nmax)) {

      b->StartDomino();

      // esperar trigger
      while (b->IsBusy()) {
         if (g_stop) break;
         dormir_ms(1);
         if (terminal) {
            auto ahora = std::chrono::steady_clock::now();
            if (std::chrono::duration<double>(ahora - t_ultimo_latido).count() >= 10) {
               t_ultimo_latido = ahora;
               double el = std::chrono::duration<double>(ahora - t_inicio).count();
               printf("\r  esperando trigger... %s transcurrido, %ld eventos     ", hms(el).c_str(), nev);
               fflush(stdout);
               linea_latido = true;
            }
         }
      }
      if (g_stop) break;

      std::string ts = now_timestamp();

      b->TransferWaves(0, 8);
      int tc = b->GetTriggerCell(0);

      ResultadoCanal res[4];   // indices 1..3
      for (int ch : c.canales) {
         int idx = 2 * (ch - 1);           // CH1..CH3 -> canales 0,2,4 del chip
         b->GetTime(0, idx, tc, tiempo[ch]);   // eje temporal calibrado DE ESTE canal
         b->GetWave(0, idx, onda[ch]);         // mV
         res[ch] = analizar_canal(tiempo[ch], onda[ch], kNBins, c.thr_mV, c.fraccion, c.nskip, c.nbase);
      }

      double dt31 = res[3].t_cfd - res[1].t_cfd;   // NaN si falta alguno
      double dt21 = res[2].t_cfd - res[1].t_cfd;
      double dt32 = res[3].t_cfd - res[2].t_cfd;

      // rotacion
      if (W.hay_que_rotar()) {
         if (!W.abrir(c, e)) { g_stop = 1; break; }
      }

      // CSV de eventos
      fprintf(W.fev, "%ld,%s", nev, ts.c_str());
      for (int ch = 1; ch <= 3; ch++) fprintf(W.fev, ",%s", fnum(res[ch].t_cfd, 4).c_str());
      for (int ch = 1; ch <= 3; ch++) fprintf(W.fev, ",%s", fnum(res[ch].t_le, 4).c_str());
      for (int ch = 1; ch <= 3; ch++) fprintf(W.fev, ",%s", fnum(res[ch].amp, 2).c_str());
      for (int ch = 1; ch <= 3; ch++) fprintf(W.fev, ",%s", fnum(res[ch].base, 2).c_str());
      fprintf(W.fev, ",%s,%s,%s\n", fnum(dt31, 4).c_str(), fnum(dt21, 4).c_str(), fnum(dt32, 4).c_str());
      fflush(W.fev);

      // formas de onda
      if (W.fondas) {
         fprintf(W.fondas, "Event #%ld  ts=%s\n", nev, ts.c_str());
         for (int i = 0; i < kNBins; i++) {
            for (size_t k = 0; k < c.canales.size(); k++) {
               int ch = c.canales[k];
               fprintf(W.fondas, k ? " %9.4f %7.1f" : "%9.4f %7.1f", tiempo[ch][i], onda[ch][i]);
            }
            fputc('\n', W.fondas);
         }
         fputc('\n', W.fondas);
         fflush(W.fondas);
      }
      W.events_in_file++;
      nev++;

      // estadistica corriente de dt31
      if (std::isfinite(dt31)) {
         n_dt31++;
         double delta = dt31 - media_dt31;
         media_dt31 += delta / n_dt31;
         m2_dt31 += delta * (dt31 - media_dt31);
      }

      // resumen en pantalla
      double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_inicio).count();
      double tasa = el > 0 ? nev / (el / 3600.0) : 0;
      if (linea_latido) { printf("\r%80s\r", ""); linea_latido = false; }
      printf("ev %5ld | %s | %s | %6.1f ev/h | dt31=%9s ns | A1=%7s A2=%7s A3=%7s mV",
             nev, ts.c_str() + 11, hms(el).c_str(), tasa, fnum(dt31, 3).c_str(),
             fnum(res[1].amp, 1).c_str(), fnum(res[2].amp, 1).c_str(), fnum(res[3].amp, 1).c_str());
      if (n_dt31 >= 2) {
         double sd = sqrt(m2_dt31 / (n_dt31 - 1));
         printf(" | <dt31>=%.3f+-%.3f (n=%ld)", media_dt31, sd / sqrt((double)n_dt31), n_dt31);
      }
      printf("\n");
      fflush(stdout);
   }

   if (g_stop)
      printf("\n\nSenal de corte recibida: cerrando archivos...\n");

   W.cerrar();

   double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_inicio).count();
   printf("\n==================== RESUMEN ====================\n");
   printf(" Eventos guardados:   %ld en %s (%.2f ev/h)\n", nev, hms(el).c_str(),
          el > 0 ? nev / (el / 3600.0) : 0.0);
   printf(" Eventos con dt31:    %ld\n", n_dt31);
   if (n_dt31 >= 2) {
      double sd = sqrt(m2_dt31 / (n_dt31 - 1));
      printf(" dt31 medio:          %.3f ns, desviacion %.3f ns, error %.3f ns\n",
             media_dt31, sd, sd / sqrt((double)n_dt31));
   }
   printf(" Archivos:            %s_idx0 ... _idx%d\n", W.base.c_str(), W.file_index - 1);
   printf("=================================================\n");

   delete drs;
   return 0;
}

/*==================== main ====================*/

int main(int argc, char *argv[])
{
   if (argc < 2) { ayuda_general(); return 1; }

   std::string sub = argv[1];
   if (sub == "--help" || sub == "-h" || sub == "help" || sub == "ayuda") { ayuda_general(); return 0; }
   if (sub == "--version") { printf("%s\n", kVersion); return 0; }
   if (sub == "info")  return cmd_info(argc, argv);
   if (sub == "calib") return cmd_calib(argc, argv);
   if (sub == "medir") return cmd_medir(argc, argv);

   fprintf(stderr, "ERROR: subcomando desconocido '%s'\n\n", sub.c_str());
   ayuda_general();
   return 1;
}
