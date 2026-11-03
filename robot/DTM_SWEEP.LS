/PROG  DTM_SWEEP
/ATTR
OWNER		= MNEDITOR;
COMMENT		= "DTM FULL RANGE SWEEP";
PROG_SIZE	= 0;
CREATE		= DATE 26-10-16  TIME 00:00:00;
MODIFIED	= DATE 26-10-16  TIME 00:00:00;
FILE_NAME	= ;
VERSION		= 0;
LINE_COUNT	= 38;
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
   1:  !DTM Pro P7: full-range slow sweep ;
   2:  !Run weekly, after warm-up, same ;
   3:  !payload. Run DTM_STREAM (Pro) first. ;
   4:  !R[114] = 8000 + axis*10 + dir ;
   5:  !dir 1 = MIN->MAX, 2 = MAX->MIN ;
   6:  !Teach PR[61..64] as JOINT: only the ;
   7:  !tested axis differs; widest range ;
   8:  !without interference. Copy a block ;
   9:  !for other axes (PR[65..], 80x1/80x2). ;
  10:  OVERRIDE=100%    ;
  11:  PAYLOAD[1]    ;
  12:  R[114:DTM_LINE]=0    ;
  13:  J PR[50:DTM_HOME] 20% FINE    ;
  14:  !---- J2 ---- ;
  15:  J PR[61:J2_MIN] 20% FINE    ;
  16:  WAIT   1.00(sec)    ;
  17:  R[114:DTM_LINE]=8021    ;
  18:  J PR[62:J2_MAX] 5% FINE    ;
  19:  R[114:DTM_LINE]=0    ;
  20:  WAIT    .50(sec)    ;
  21:  R[114:DTM_LINE]=8022    ;
  22:  J PR[61:J2_MIN] 5% FINE    ;
  23:  R[114:DTM_LINE]=0    ;
  24:  WAIT    .50(sec)    ;
  25:  J PR[50:DTM_HOME] 20% FINE    ;
  26:  !---- J3 ---- ;
  27:  J PR[63:J3_MIN] 20% FINE    ;
  28:  WAIT   1.00(sec)    ;
  29:  R[114:DTM_LINE]=8031    ;
  30:  J PR[64:J3_MAX] 5% FINE    ;
  31:  R[114:DTM_LINE]=0    ;
  32:  WAIT    .50(sec)    ;
  33:  R[114:DTM_LINE]=8032    ;
  34:  J PR[63:J3_MIN] 5% FINE    ;
  35:  R[114:DTM_LINE]=0    ;
  36:  WAIT    .50(sec)    ;
  37:  J PR[50:DTM_HOME] 20% FINE    ;
  38:  R[114:DTM_LINE]=0    ;
/POS
/END
