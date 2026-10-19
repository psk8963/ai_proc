/* 공통 헤더: 길이 상수와 고객 구조체 */
#ifndef COMMON_H
#define COMMON_H

#define LEN_CUST_ID   10
#define LEN_CUST_NM   20
#define LEN_STATUS    2

typedef struct {
    char   cust_id[LEN_CUST_ID + 1];
    char   cust_nm[LEN_CUST_NM + 1];
    char   grade[2];
    double credit_amt;
} CUST_REC;

#define SQL_NOT_FOUND 1403

#endif
