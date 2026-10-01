/********************************************************************\

  Name:         drs_exam.cpp
  Modified:     Add per-event selection, timestamps, and file rolling

\********************************************************************/
 
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <iostream>

#include <chrono>     // NEW: timestamps & rotation timer
#include <ctime>      // NEW
#include <string>     // NEW
#include <sstream>    // NEW
#include <iomanip>    // NEW

#ifdef _MSC_VER
  #include <windows.h>
#elif defined(OS_LINUX)
  #define O_BINARY 0
  #include <unistd.h>
  #include <ctype.h>
  #include <sys/ioctl.h>
  #include <errno.h>
  #define DIR_SEPARATOR '/'
#endif

#include "strlcpy.h"
#include "DRS.h"


const double kThr   = -100 ; // En mVolts
const int kn_save   = 1024;
const bool save_all = false;
const std::string outDir = "./medidas/"; 


/*==================== NEW: small helpers ====================*/

// roll limits (tweak as you like)
static const int   MAX_EVENTS_PER_FILE = 2000;        // rotate after this many events
static const int   MAX_SECS_PER_FILE   = 300;        // or after this many seconds

// Format absolute timestamp with milliseconds: "YYYY-MM-DD HH:MM:SS.mmm"
static std::string now_timestamp()
{
  using namespace std::chrono;
  auto now = system_clock::now();
  auto t   = system_clock::to_time_t(now);
  auto ms  = duration_cast<milliseconds>(now.time_since_epoch()) % 1000;

  std::tm tm{};
#if defined(_MSC_VER)
  localtime_s(&tm, &t);
#else
  localtime_r(&t, &tm);
#endif

  std::ostringstream oss;
  oss << std::setfill('0')
      << std::setw(4) << (tm.tm_year + 1900) << "-"
      << std::setw(2) << (tm.tm_mon + 1)     << "-"
      << std::setw(2) << tm.tm_mday          << " "
      << std::setw(2) << tm.tm_hour          << ":"
      << std::setw(2) << tm.tm_min           << ":"
      << std::setw(2) << tm.tm_sec           << "."
      << std::setw(3) << (int)ms.count();
  return oss.str();
}

// Build a base name like: "run_YYYYmmdd_HHMMSS"
static std::string make_run_id()
{
  using namespace std::chrono;
  auto now = system_clock::now();
  auto t   = system_clock::to_time_t(now);
  std::tm tm{};
#if defined(_MSC_VER)
  localtime_s(&tm, &t);
#else
  localtime_r(&t, &tm);
#endif
  char buf[32];
  std::snprintf(buf, sizeof(buf), "%04d%02d%02d_%02d%02d%02d",
                tm.tm_year+1900, tm.tm_mon+1, tm.tm_mday,
                tm.tm_hour, tm.tm_min, tm.tm_sec);
  return outDir + std::string("run_") + buf;
}

// Simple placeholder criterion: true if any channel’s absolute peak < thr (in mV)
// Replace with your real physics selection later.
static bool passes_criteria(const float *u, int n_min, int n_max, float thr = 50.0f)
//{
//  for (int i=n_min;i<n_max;i++) {
//    if (u[i] < thr){ 
//	    return true;
//    }
//  }
//  return false;
//}
{
  for (int i=n_min;i<n_max;i++) {
    if (u[i] < thr){ 
		for (int j=-10;j<31;j++) { //nuevo criterio, tira las mediciones que tengan un punto por encima de -thr, esquiva overloads ^^
			if (u[i+j] > 70) {
				return false;}
			}
	    return true;
    }
  }
  return false;
}

/* A tiny file-rotator that keeps two FILE*s (all events + selected events) */
struct RotatingWriters {
  FILE *fall   = nullptr;  // all events
  FILE *fsel   = nullptr;  // selected events
  std::string run_id;      // timestamped base id at open
  int file_index = 0;      // 0,1,2,...
  int events_in_file = 0;
  std::chrono::steady_clock::time_point opened_at;

  void close_all() {
    if (fall) { fclose(fall); fall=nullptr; }
    if (fsel) { fclose(fsel); fsel=nullptr; }
  }

  void write_header(FILE* f) {
    if (!f) return;
    // (Fixed extra bracket in u2 header)
    fprintf(f, "# DRS4 export. File index %d, run_id=%s\n", file_index, run_id.c_str());
    fprintf(f, "# Columns: t[ns] u1[mV] u2[mV] u3[mV]\n");
  }

  bool open_new_files()
  {
    close_all();
    if (file_index == 0) run_id = make_run_id();

    std::ostringstream name_all, name_sel;
    name_all << run_id << "_idx" << file_index << ".txt";
    name_sel << run_id << "_idx" << file_index << "_selected.txt";

    if(save_all){
	    fall = fopen(name_all.str().c_str(), "w");
	    if (!fall) { perror("ERROR: Cannot open output file (all)"); return false; }
    } else {
	    fall = nullptr;
    }

    fsel = fopen(name_sel.str().c_str(), "w");
    if (!fsel) { perror("ERROR: Cannot open output file (selected)"); fclose(fall); fall=nullptr; return false; }

    write_header(fall);
    write_header(fsel);

    events_in_file = 0;
    opened_at = std::chrono::steady_clock::now();
    file_index++;
    return true;
  }

  bool need_rotate() const
  {
    using namespace std::chrono;
    if (events_in_file >= MAX_EVENTS_PER_FILE) return true;
    auto secs = duration_cast<seconds>(steady_clock::now() - opened_at).count();
    return secs >= MAX_SECS_PER_FILE;
  }
};

/*==================== END helpers ====================*/

int main()
{
  int i, j, nBoards;
  DRS *drs;
  DRSBoard *b;
  float time_array[8][1024];
  float wave_array[8][1024];

  drs = new DRS();

  // Show boards
  for (i=0 ; i<drs->GetNumberOfBoards() ; i++) {
    b = drs->GetBoard(i);
    printf("Found DRS4 evaluation board, serial #%d, firmware revision %d\n",
      b->GetBoardSerialNumber(), b->GetFirmwareVersion());
  }

  nBoards = drs->GetNumberOfBoards();
  if (nBoards == 0) {
    printf("No DRS4 evaluation board found\n");
    delete drs;            // tidy
    return 0;
  }

  // Use first board
  b = drs->GetBoard(0);

  // Init
  b->Init();

  // Sampling frequency & modes (unchanged)
  b->SetFrequency(0.6, true);
  b->SetTranspMode(1);
  b->SetInputRange(0);
  b->EnableTcal(0);        // NOTE: use your real inputs; disable TCAL for real data

  // Trigger on CH1 (example). Adjust if you want multi-channel OR.
  if (b->GetBoardType() >= 8) {
    b->EnableTrigger(1, 0);
    b->SetTriggerSource(1<<0);
  } else if (b->GetBoardType() == 7) {
    b->EnableTrigger(0, 1);
    b->SetTriggerSource(0);
  }
  b->SetTriggerLevel(kThr/1000.); // toma el parametro en volts
  b->SetTriggerPolarity(false);
  b->SetTriggerDelayNs(1740);  //1740

  // NEW: rotating writers for (all events) and (selected events)
  RotatingWriters W;
  if (!W.open_new_files()) { delete drs; return 1; }

  // Repeat many times (increase from 10 to more; rotation handles files)
  for (j=0 ; j<1000000 ; j++) {

    // Rotate files if needed (by count or time)
    if (W.need_rotate()) {
      if (!W.open_new_files()) { delete drs; return 1; }
    }

    // Start & wait for trigger
    b->StartDomino();
    printf("Waiting for trigger..."); fflush(stdout);
#if defined(OS_LINUX)
    while (b->IsBusy()) usleep(1000);   // be kind to CPU
#else
    while (b->IsBusy());
#endif

    // Transfer and decode the three inputs you were already using
    b->TransferWaves(0, 8);

    // Time arrays (ns) and waveforms (mV)
    b->GetTime(0, 0, b->GetTriggerCell(0), time_array[0]);
    b->GetWave(0, 0, wave_array[0]);
    b->GetWave(0, 2, wave_array[1]);
    b->GetWave(0, 4, wave_array[2]);

    // NEW: Event absolute timestamp
    std::string ts = now_timestamp();

    if (save_all){
	    // Write event to the "all events" file
	    fprintf(W.fall, "Event #%d  ts=%s\n", j, ts.c_str());
	    fprintf(W.fall, "  tiempo[ns]  u1[mV]  u2[mV]  u3[mV]\n");
	    for (i=0 ; i<1024 ; i++)
	      fprintf(W.fall, "%7.3f %7.1f %7.1f %7.1f\n",
        	      time_array[0][i], wave_array[0][i], wave_array[1][i], wave_array[2][i]);
	    fprintf(W.fall, "\n");
    	fflush(W.fall); // optional: ensure on-disk promptly
    }

    // NEW: Apply selection
    // Ch-1 is the one that triggers
    // here we check if Ch-2 Ch-3 also have a peak
    const bool peakCh2 =   passes_criteria(wave_array[1],100,150, kThr);
    //~ const bool peakCh3 =   passes_criteria(wave_array[2],100,150, kThr);  
    //~ std::cout << "   Sel: " << peakCh2 << " " << peakCh3 << std::endl;   
    std::cout << "   Sel: " << peakCh2 << std::endl;
    //~ if (peakCh2*peakCh3) {
    if (peakCh2) {
      printf("   Evento seleccionado!\n");
      fprintf(W.fsel, "Event #%d  ts=%s  [SELECTED]\n", j, ts.c_str());
      fprintf(W.fsel, "  t[ns]  u1[mV]  u2[mV]  u3[mV]\n");
      for (i=0 ; i<kn_save; i++)
        fprintf(W.fsel, "%7.3f %7.1f %7.1f %7.1f\n",
                time_array[0][i], wave_array[0][i], wave_array[1][i], wave_array[2][i]);
      fprintf(W.fsel, "\n");
      fflush(W.fsel);
    }

    W.events_in_file++;

    printf("\rEvent #%d written (ts=%s)          \n", j, ts.c_str());
  }

  W.close_all();
  delete drs;
  return 0;
}
