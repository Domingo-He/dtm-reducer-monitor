/PROG  DTM_MCODE_EXAMPLE
/ATTR
OWNER		= MNEDITOR;
COMMENT		= "MOTION CODE EXAMPLE";
PROG_SIZE	= 0;
CREATE		= DATE 26-10-16  TIME 00:00:00;
MODIFIED	= DATE 26-10-16  TIME 00:00:00;
FILE_NAME	= ;
VERSION		= 0;
LINE_COUNT	= 0;
MEMORY_SIZE	= 0;
PROTECT		= READ_WRITE;
TCD:  STACK_SIZE	= 0,
      TASK_PRIORITY	= 50,
      TIME_SLICE	= 0,
      BUSY_LAMP_OFF	= 0,
      ABORT_REQUEST	= 0,
      PAUSE_REQUEST	= 0;
DEFAULT_GROUP	= 1,*,*,*,*;
CONTROL_CODE	= 00000000 00000000;
/MN
   1:  !EXAMPLE ONLY - copy the R[110] lines ;
   2:  !into your own production program. ;
   3:  !Motion code 9000-9899 marks a fixed ;
   4:  !motion so DTM compares the same ;
   5:  !motion every cycle (daily median). ;
   6:  !Rules: ;
   7:  !- FINE at both ends of the motion ;
   8:  !- loaded and empty moves use ;
   9:  !  different codes (9001 / 9002) ;
  10:  !- do not mark conveyor tracking ;
  11:  !- after changing these points or ;
  12:  !  speeds, register a motion ;
  13:  !  baseline reset in DTM ;
  14:  J P[1:PICK] 100% FINE    ;
  15:  R[110:MOTION_ID]=9001    ;
  16:  J P[2:PLACE] 80% FINE    ;
  17:  R[110:MOTION_ID]=0    ;
  18:  J P[3:WAIT] 100% FINE    ;
  19:  R[110:MOTION_ID]=9002    ;
  20:  J P[1:PICK] 100% FINE    ;
  21:  R[110:MOTION_ID]=0    ;
/POS
P[1]{
   GP1:
	UF : 0, UT : 1,
	J1=     0.000 deg,	J2=     0.000 deg,	J3=     0.000 deg,
	J4=     0.000 deg,	J5=   -90.000 deg,	J6=     0.000 deg
};
/END
