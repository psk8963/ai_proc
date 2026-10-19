#!/bin/ksh
# 일 마감 주문 배치
. $HOME/.profile
WORK_DT=`date +%Y%m%d`
LOG=$LOG_DIR/daily_ord_$WORK_DT.log

echo "start" >> $LOG

$BIN_DIR/ORD010 $WORK_DT >> $LOG 2>&1
if [ $? -ne 0 ]; then
    echo "ORD010 failed" >> $LOG
    exit 1
fi

sh $SH_DIR/daily_sum.sh $WORK_DT || exit 2

sqlplus -s $DB_USER/$DB_PASS @$SQL_DIR/cleanup.sql

echo "end" >> $LOG
