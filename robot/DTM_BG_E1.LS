/PROG  DTM_BG_E1
/ATTR
OWNER		= MNEDITOR;
COMMENT		= "DIST TRQ J1-E1 TO REG";
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
DEFAULT_GROUP	= *,*,*,*,*;
CONTROL_CODE	= 00000000 00000000;
/MN
   1:  !Background Logic for robots WITH ;
   2:  !a 7th axis (travel axis E1) ;
   3:  !Copy disturbance torque J1-J6 and E1 ;
   4:  !into R[101]-R[107] ;
   5:  !Run under MENU>SETUP>BG Logic ;
   6:  R[101:DT_J1]=$MOR_GRP[1].$CUR_DIS_TRQ[1]    ;
   7:  R[102:DT_J2]=$MOR_GRP[1].$CUR_DIS_TRQ[2]    ;
   8:  R[103:DT_J3]=$MOR_GRP[1].$CUR_DIS_TRQ[3]    ;
   9:  R[104:DT_J4]=$MOR_GRP[1].$CUR_DIS_TRQ[4]    ;
  10:  R[105:DT_J5]=$MOR_GRP[1].$CUR_DIS_TRQ[5]    ;
  11:  R[106:DT_J6]=$MOR_GRP[1].$CUR_DIS_TRQ[6]    ;
  12:  !E1 as integrated axis 7 of group 1: ;
  13:  R[107:DT_E1]=$MOR_GRP[1].$CUR_DIS_TRQ[7]    ;
  14:  !If E1 is a separate motion group 2, ;
  15:  !change the line above to: ;
  16:  !R[107]=$MOR_GRP[2].$CUR_DIS_TRQ[1] ;
  17:  !Heartbeat: PC checks it is changing ;
  18:  R[112:DTM_HB]=(R[112:DTM_HB]+1) MOD 10000    ;
  19:  !General override, must be 100 in diag ;
  20:  R[113:DTM_OVR]=$MCR.$GENOVERRIDE    ;
/POS
/END
