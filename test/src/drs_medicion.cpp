/********************************************************************\

  Name:         drs_exam_multi.cpp
  Created by:   Stefan Ritt

  Contents:     Simple example application to read out a several
                DRS4 evaluation board in daisy-chain mode

  $Id: drs_exam_multi.cpp 21509 2014-10-15 10:11:36Z ritt $

\********************************************************************/

#include <math.h>

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

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include <iostream>

#include "strlcpy.h"
#include "DRS.h"
/*------------------------------------------------------------------*/


int main()
{
   int i, j, k;
   DRS *drs;
   DRSBoard *b, *mb;
   float time_array[8][1024];
   float wave_array[8][1024];
   FILE  *f;

   /* do initial scan, sort boards accordning to their serial numbers */
   drs = new DRS();
   drs->SortBoards();

   /* show any found board(s) */
   for (i=0 ; i<drs->GetNumberOfBoards() ; i++) {
      b = drs->GetBoard(i);
      std::cout << "board type: " << b->GetBoardType() << std::endl;
      printf("Found DRS4 evaluation board, serial #%d, firmware revision %d\n", 
         b->GetBoardSerialNumber(), b->GetFirmwareVersion());
      printf("Board type: %d\n ",b->GetBoardType());
      if (b->GetBoardType() < 8) {
         printf("Found pre-V4 board, aborting\n");
         return 0;
      }
   }
   
   /* exit if no board found */
   if (drs->GetNumberOfBoards() == 0) {
      printf("No DRS4 evaluation board found\n");
      return 0;
   }

   /* exit if only one board found */
   //if (drs->GetNumberOfBoards() == 1) {
   //   printf("Only one DRS4 evaluation board found, please use drs_exam program\n");
   //   return 0;
   //}
   
   /* use first board with highest serial number as the master board */
   mb = drs->GetBoard(0);
   
   /* common configuration for all boards */
   for (i=0 ; i<drs->GetNumberOfBoards() ; i++) {
      b = drs->GetBoard(i);
          
      /* initialize board */
      b->Init();
      /* select external reference clock for slave modules */
      /* NOTE: this only works if the clock chain is connected */
      if (i > 0) {
         if (b->GetFirmwareVersion() >= 21260) { // this only works with recent firmware versions
            if (b->GetScaler(5) > 300000)        // check if external clock is connected
               b->SetRefclk(true);               // switch to external reference clock
         }
      }
      
      /* set sampling frequency */
      b->SetFrequency(0.6, true); // usar 0.6
      
      /* set input range to -0.5V ... +0.5V */
      b->SetInputRange(0);

      /* enable hardware trigger */
      b->EnableTrigger(1, 0);
      
      if (i == 0) {
         /* master board: enable hardware trigger on CH1 at 50 mV positive edge */
         b->SetTranspMode(1);
         b->SetTriggerSource(1<<0);        	// set CH1 as source
         b->SetTriggerLevel(-0.06);       	// [V] 0.15 en siberia, 0.1 en lambda y otros lugares
         b->SetTriggerPolarity(false);     	// positive edge
         b->SetTriggerDelayNs(2000);       	// ns trigger delay
      } else {
         /* slave boards: enable hardware trigger on Trigger IN */
         b->SetTriggerSource(1<<4);        // set Trigger IN as source
         b->SetTriggerPolarity(false);     // positive edge
      }
   }

   char filename[100];
   /* repeat ten times */
   for (i=0 ; i<300000 ; i++) { 
      sprintf(filename, "mediciones/data.txt");
      FILE* f = fopen(filename, "w");
      //f = fopen("mediciones/data_%i.txt", "w");
      /* open file to save waveforms */
      if (f == NULL) {
         perror("ERROR: Cannot open file \"data.txt\"");
         return 1;
      }

      /* start boards (activate domino wave), master is last */
      for (j=drs->GetNumberOfBoards()-1 ; j>=0 ; j--)
         drs->GetBoard(j)->StartDomino();

      /* wait for trigger on master board */
      printf("Waiting for trigger...");
      fflush(stdout);
      while (mb->IsBusy());

      //fprintf(f, "Event #%d =================\n", j);

      for (j=0 ; j<drs->GetNumberOfBoards() ; j++) {
         b = drs->GetBoard(j);
         if (b->IsBusy()) {
            i--; /* skip that event, must be some fake trigger */
            break;
         }
         
         /* read all waveforms from all boards */
         b->TransferWaves(0, 8);
         
         for (k=0 ; k<4 ; k++) {
            /* read time (X) array in ns */
            b->GetTime(0, k*2, b->GetTriggerCell(0), time_array[k]);

            /* decode waveform (Y) arrays in mV */
            b->GetWave(0, k*2, wave_array[k]);
         }

         /* Save waveform: X=time_array[i], Channel_n=wave_array[n][i] */
         //fprintf(f, "Board #%d ---------------------------------------------------\n t1[ns]  u1[mV]  t2[ns]  u2[mV]  t3[ns]  u3[mV]  t4[ns]  u4[mV]\n", b->GetBoardSerialNumber());
         //for (k=0 ; k<1024 ; k++)
         //   fprintf(f, "%7.3f %7.1f %7.3f %7.1f %7.3f %7.1f %7.3f %7.1f\n",
         //           time_array[0][k], wave_array[0][k],
         //           time_array[1][k], wave_array[1][k],
         //           time_array[2][k], wave_array[2][k],
         //           time_array[3][k], wave_array[3][k]);
         for (k=0 ; k<1024 ; k++)
            fprintf(f, "%7.1f\t%7.1f\t%7.1f\t%7.1f\t%7.1f\t%7.1f\n",
                    time_array[0][k], wave_array[0][k],
                    time_array[1][k], wave_array[1][k],
                    time_array[2][k], wave_array[2][k]);
	 sleep(1);
         fclose(f);
      }

      /* print some progress indication */
      printf("\rEvent #%d read successfully\n", i);
   }

   fclose(f);
   
   printf("Program finished.\n");

   /* delete DRS object -> close USB connection */
   delete drs;
}
