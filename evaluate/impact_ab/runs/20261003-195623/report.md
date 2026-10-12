# Impact benchmark, run 20261003-195623

## hard

| Braço | Execuções | precision | recall | f1 | file_recall | total_cost_usd | num_turns | wall_seconds | graph_tool_calls |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 20 | 0.969 | 1.000 | 0.982 | 1.000 | 0.062 | 6.200 | 17.195 | 0.000 |
| graph | 20 | 0.969 | 1.000 | 0.982 | 1.000 | 0.068 | 6.250 | 18.780 | 1.000 |

Diferença pareada por alvo (graph − baseline), 10 alvos, IC 95% bootstrap:

| Métrica | Diferença | IC 95% | Melhor | Pior |
|---|---|---|---|---|
| precision | +0.000 | [+0.000, +0.000] | 0 | 0 |
| recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| f1 | +0.000 | [+0.000, +0.000] | 0 | 0 |
| file_recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| total_cost_usd | +0.006 | [-0.008, +0.018] | 8 | 2 |
| num_turns | +0.050 | [-0.500, +0.550] | 3 | 3 |
| wall_seconds | +1.585 | [-0.020, +3.085] | 7 | 3 |

## easy

| Braço | Execuções | precision | recall | f1 | file_recall | total_cost_usd | num_turns | wall_seconds | graph_tool_calls |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 8 | 1.000 | 1.000 | 1.000 | 1.000 | 0.027 | 4.500 | 12.512 | 0.000 |
| graph | 8 | 1.000 | 1.000 | 1.000 | 1.000 | 0.036 | 4.375 | 15.238 | 1.000 |

Diferença pareada por alvo (graph − baseline), 4 alvos, IC 95% bootstrap:

| Métrica | Diferença | IC 95% | Melhor | Pior |
|---|---|---|---|---|
| precision | +0.000 | [+0.000, +0.000] | 0 | 0 |
| recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| f1 | +0.000 | [+0.000, +0.000] | 0 | 0 |
| file_recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| total_cost_usd | +0.009 | [+0.002, +0.014] | 3 | 1 |
| num_turns | -0.125 | [-1.000, +0.500] | 2 | 1 |
| wall_seconds | +2.725 | [-0.525, +4.600] | 3 | 1 |

## all

| Braço | Execuções | precision | recall | f1 | file_recall | total_cost_usd | num_turns | wall_seconds | graph_tool_calls |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 28 | 0.978 | 1.000 | 0.987 | 1.000 | 0.052 | 5.714 | 15.857 | 0.000 |
| graph | 28 | 0.978 | 1.000 | 0.987 | 1.000 | 0.059 | 5.714 | 17.768 | 1.000 |

Diferença pareada por alvo (graph − baseline), 14 alvos, IC 95% bootstrap:

| Métrica | Diferença | IC 95% | Melhor | Pior |
|---|---|---|---|---|
| precision | +0.000 | [+0.000, +0.000] | 0 | 0 |
| recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| f1 | +0.000 | [+0.000, +0.000] | 0 | 0 |
| file_recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| total_cost_usd | +0.007 | [-0.003, +0.016] | 11 | 3 |
| num_turns | +0.000 | [-0.429, +0.429] | 5 | 4 |
| wall_seconds | +1.911 | [+0.493, +3.254] | 10 | 4 |

## Por alvo

| Alvo | Grupo | Chamadas | Braço | Reportadas | VP | Precisão | Recall | Custo | Turnos |
|---|---|---|---|---|---|---|---|---|---|
| t01-formatCurrency | hard | 11 | baseline r0 | 11 | 11 | 1.00 | 1.00 | $0.16 | 10 |
| t01-formatCurrency | hard | 11 | baseline r1 | 11 | 11 | 1.00 | 1.00 | $0.10 | 8 |
| t01-formatCurrency | hard | 11 | graph r0 | 11 | 11 | 1.00 | 1.00 | $0.08 | 8 |
| t01-formatCurrency | hard | 11 | graph r1 | 11 | 11 | 1.00 | 1.00 | $0.08 | 8 |
| t02-prefersReducedMotion | hard | 23 | baseline r0 | 23 | 23 | 1.00 | 1.00 | $0.06 | 8 |
| t02-prefersReducedMotion | hard | 23 | baseline r1 | 23 | 23 | 1.00 | 1.00 | $0.06 | 7 |
| t02-prefersReducedMotion | hard | 23 | graph r0 | 23 | 23 | 1.00 | 1.00 | $0.07 | 6 |
| t02-prefersReducedMotion | hard | 23 | graph r1 | 23 | 23 | 1.00 | 1.00 | $0.09 | 11 |
| t03-BRL | hard | 37 | baseline r0 | 37 | 37 | 1.00 | 1.00 | $0.10 | 5 |
| t03-BRL | hard | 37 | baseline r1 | 37 | 37 | 1.00 | 1.00 | $0.08 | 5 |
| t03-BRL | hard | 37 | graph r0 | 37 | 37 | 1.00 | 1.00 | $0.10 | 8 |
| t03-BRL | hard | 37 | graph r1 | 37 | 37 | 1.00 | 1.00 | $0.10 | 5 |
| t04-formatDate | hard | 6 | baseline r0 | 6 | 6 | 1.00 | 1.00 | $0.06 | 7 |
| t04-formatDate | hard | 6 | baseline r1 | 6 | 6 | 1.00 | 1.00 | $0.08 | 8 |
| t04-formatDate | hard | 6 | graph r0 | 6 | 6 | 1.00 | 1.00 | $0.07 | 6 |
| t04-formatDate | hard | 6 | graph r1 | 6 | 6 | 1.00 | 1.00 | $0.06 | 7 |
| t05-formatBRL | hard | 10 | baseline r0 | 10 | 10 | 1.00 | 1.00 | $0.04 | 4 |
| t05-formatBRL | hard | 10 | baseline r1 | 10 | 10 | 1.00 | 1.00 | $0.05 | 4 |
| t05-formatBRL | hard | 10 | graph r0 | 10 | 10 | 1.00 | 1.00 | $0.07 | 6 |
| t05-formatBRL | hard | 10 | graph r1 | 10 | 10 | 1.00 | 1.00 | $0.05 | 4 |
| t06-useOperationalListFilterQueryState | hard | 9 | baseline r0 | 13 | 9 | 0.69 | 1.00 | $0.04 | 4 |
| t06-useOperationalListFilterQueryState | hard | 9 | baseline r1 | 13 | 9 | 0.69 | 1.00 | $0.05 | 4 |
| t06-useOperationalListFilterQueryState | hard | 9 | graph r0 | 13 | 9 | 0.69 | 1.00 | $0.09 | 4 |
| t06-useOperationalListFilterQueryState | hard | 9 | graph r1 | 13 | 9 | 0.69 | 1.00 | $0.09 | 4 |
| t07-uid | hard | 6 | baseline r0 | 6 | 6 | 1.00 | 1.00 | $0.04 | 6 |
| t07-uid | hard | 6 | baseline r1 | 6 | 6 | 1.00 | 1.00 | $0.06 | 6 |
| t07-uid | hard | 6 | graph r0 | 6 | 6 | 1.00 | 1.00 | $0.04 | 5 |
| t07-uid | hard | 6 | graph r1 | 6 | 6 | 1.00 | 1.00 | $0.06 | 7 |
| t08-uid | hard | 7 | baseline r0 | 7 | 7 | 1.00 | 1.00 | $0.05 | 8 |
| t08-uid | hard | 7 | baseline r1 | 7 | 7 | 1.00 | 1.00 | $0.05 | 8 |
| t08-uid | hard | 7 | graph r0 | 7 | 7 | 1.00 | 1.00 | $0.05 | 8 |
| t08-uid | hard | 7 | graph r1 | 7 | 7 | 1.00 | 1.00 | $0.06 | 8 |
| t09-canAccessGlobalSearch | hard | 8 | baseline r0 | 8 | 8 | 1.00 | 1.00 | $0.03 | 5 |
| t09-canAccessGlobalSearch | hard | 8 | baseline r1 | 8 | 8 | 1.00 | 1.00 | $0.03 | 5 |
| t09-canAccessGlobalSearch | hard | 8 | graph r0 | 8 | 8 | 1.00 | 1.00 | $0.03 | 4 |
| t09-canAccessGlobalSearch | hard | 8 | graph r1 | 8 | 8 | 1.00 | 1.00 | $0.04 | 4 |
| t10-getPendenciasData | hard | 6 | baseline r0 | 6 | 6 | 1.00 | 1.00 | $0.05 | 7 |
| t10-getPendenciasData | hard | 6 | baseline r1 | 6 | 6 | 1.00 | 1.00 | $0.04 | 5 |
| t10-getPendenciasData | hard | 6 | graph r0 | 6 | 6 | 1.00 | 1.00 | $0.05 | 6 |
| t10-getPendenciasData | hard | 6 | graph r1 | 6 | 6 | 1.00 | 1.00 | $0.05 | 6 |
| t11-sanitizeCrmRqlLiteral | easy | 10 | baseline r0 | 10 | 10 | 1.00 | 1.00 | $0.02 | 5 |
| t11-sanitizeCrmRqlLiteral | easy | 10 | baseline r1 | 10 | 10 | 1.00 | 1.00 | $0.03 | 5 |
| t11-sanitizeCrmRqlLiteral | easy | 10 | graph r0 | 10 | 10 | 1.00 | 1.00 | $0.03 | 4 |
| t11-sanitizeCrmRqlLiteral | easy | 10 | graph r1 | 10 | 10 | 1.00 | 1.00 | $0.05 | 6 |
| t12-formatBreadcrumb | easy | 16 | baseline r0 | 16 | 16 | 1.00 | 1.00 | $0.03 | 4 |
| t12-formatBreadcrumb | easy | 16 | baseline r1 | 16 | 16 | 1.00 | 1.00 | $0.02 | 3 |
| t12-formatBreadcrumb | easy | 16 | graph r0 | 16 | 16 | 1.00 | 1.00 | $0.04 | 4 |
| t12-formatBreadcrumb | easy | 16 | graph r1 | 16 | 16 | 1.00 | 1.00 | $0.03 | 4 |
| t13-checkIsVisitorRoute | easy | 12 | baseline r0 | 12 | 12 | 1.00 | 1.00 | $0.02 | 4 |
| t13-checkIsVisitorRoute | easy | 12 | baseline r1 | 12 | 12 | 1.00 | 1.00 | $0.02 | 4 |
| t13-checkIsVisitorRoute | easy | 12 | graph r0 | 12 | 12 | 1.00 | 1.00 | $0.03 | 4 |
| t13-checkIsVisitorRoute | easy | 12 | graph r1 | 12 | 12 | 1.00 | 1.00 | $0.04 | 5 |
| t14-toHealthSegmentRatio | easy | 10 | baseline r0 | 10 | 10 | 1.00 | 1.00 | $0.05 | 7 |
| t14-toHealthSegmentRatio | easy | 10 | baseline r1 | 10 | 10 | 1.00 | 1.00 | $0.02 | 4 |
| t14-toHealthSegmentRatio | easy | 10 | graph r0 | 10 | 10 | 1.00 | 1.00 | $0.04 | 4 |
| t14-toHealthSegmentRatio | easy | 10 | graph r1 | 10 | 10 | 1.00 | 1.00 | $0.03 | 4 |
