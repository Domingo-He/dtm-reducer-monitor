/PROG  DTM_BG
/ATTR
OWNER		= MNEDITOR;
COMMENT		= "DIST TRQ TO REG";
PROG_SIZE	= 0;
CREATE		= DATE 26-10-14  TIME 00:00:00;
MODIFIED	= DATE 26-10-14  TIME 00:00:00;
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
   1:  !Background Logic: copy disturbance ;
   2:  !torque of J1-J6 into R[101]-R[106] ;
   3:  !Run under MENU>SETUP>BG Logic ;
   4:  R[101:DT_J1]=$MOR_GRP[1].$CUR_DIS_TRQ[1]    ;
   5:  R[102:DT_J2]=$MOR_GRP[1].$CUR_DIS_TRQ[2]    ;
   6:  R[103:DT_J3]=$MOR_GRP[1].$CUR_DIS_TRQ[3]    ;
   7:  R[104:DT_J4]=$MOR_GRP[1].$CUR_DIS_TRQ[4]    ;
   8:  R[105:DT_J5]=$MOR_GRP[1].$CUR_DIS_TRQ[5]    ;
   9:  R[106:DT_J6]=$MOR_GRP[1].$CUR_DIS_TRQ[6]    ;
  10:  !Heartbeat: PC checks it is changing ;
  11:  R[112:DTM_HB]=(R[112:DTM_HB]+1) MOD 10000    ;
  12:  !General override, must be 100 in diag ;
  13:  R[113:DTM_OVR]=$MCR.$GENOVERRIDE    ;
/POS
/END
