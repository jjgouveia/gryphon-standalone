# Transitive impact benchmark, run 20261003-203846-depth-claude-haiku-4-5-20251001

| Braço | Execuções | precision | recall | level1_recall | level2_recall | f1 | total_cost_usd | num_turns | wall_seconds | graph_tool_calls |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 16 | 0.926 | 0.884 | 0.967 | 0.831 | 0.897 | 0.368 | 43.188 | 3362.062 | 0.000 |
| graph | 16 | 0.924 | 0.971 | 0.984 | 0.965 | 0.944 | 0.174 | 23.250 | 1770.750 | 11.688 |

Diferença pareada por alvo (graph − baseline), 8 alvos, IC 95% bootstrap:

| Métrica | Diferença | IC 95% | Melhor | Pior |
|---|---|---|---|---|
| precision | -0.003 | [-0.058, +0.054] | 2 | 3 |
| recall | +0.087 | [+0.035, +0.140] | 5 | 0 |
| level1_recall | +0.018 | [+0.000, +0.054] | 1 | 0 |
| level2_recall | +0.133 | [+0.052, +0.206] | 5 | 0 |
| f1 | +0.047 | [+0.005, +0.087] | 5 | 2 |
| total_cost_usd | -0.194 | [-0.307, -0.090] | 1 | 7 |
| num_turns | -19.938 | [-29.625, -10.938] | 0 | 8 |
| wall_seconds | -1591.312 | [-4845.206, +134.038] | 3 | 5 |

## Por alvo

| Alvo | Verdade | Braço | Reportadas | VP | Precisão | Recall | Custo | Turnos |
|---|---|---|---|---|---|---|---|---|
| d01-getDocsFromEsteira | 19 | baseline r0 | 18 | 16 | 0.89 | 0.84 | $0.29 | 51 |
| d01-getDocsFromEsteira | 19 | baseline r1 | 18 | 16 | 0.89 | 0.84 | $0.29 | 45 |
| d01-getDocsFromEsteira | 19 | graph r0 | 19 | 16 | 0.84 | 0.84 | $0.10 | 13 |
| d01-getDocsFromEsteira | 19 | graph r1 | 19 | 16 | 0.84 | 0.84 | $0.11 | 18 |
| d02-captureCedenteFormEvent | 15 | baseline r0 | 15 | 15 | 1.00 | 1.00 | $0.39 | 34 |
| d02-captureCedenteFormEvent | 15 | baseline r1 | 9 | 9 | 1.00 | 0.60 | $0.23 | 29 |
| d02-captureCedenteFormEvent | 15 | graph r0 | 15 | 15 | 1.00 | 1.00 | $0.19 | 18 |
| d02-captureCedenteFormEvent | 15 | graph r1 | 20 | 15 | 0.75 | 1.00 | $0.22 | 29 |
| d03-PresenceAvatarStack | 9 | baseline r0 | 9 | 9 | 1.00 | 1.00 | $0.28 | 32 |
| d03-PresenceAvatarStack | 9 | baseline r1 | 9 | 9 | 1.00 | 1.00 | $0.28 | 34 |
| d03-PresenceAvatarStack | 9 | graph r0 | 9 | 9 | 1.00 | 1.00 | $0.08 | 13 |
| d03-PresenceAvatarStack | 9 | graph r1 | 9 | 9 | 1.00 | 1.00 | $0.16 | 20 |
| d04-useSmartBlockHistoricalSuggestionsQuery | 11 | baseline r0 | 8 | 8 | 1.00 | 0.73 | $0.41 | 48 |
| d04-useSmartBlockHistoricalSuggestionsQuery | 11 | baseline r1 | 10 | 10 | 1.00 | 0.91 | $0.68 | 57 |
| d04-useSmartBlockHistoricalSuggestionsQuery | 11 | graph r0 | 11 | 11 | 1.00 | 1.00 | $0.03 | 6 |
| d04-useSmartBlockHistoricalSuggestionsQuery | 11 | graph r1 | 11 | 11 | 1.00 | 1.00 | $0.08 | 10 |
| d05-usePdfViewer | 14 | baseline r0 | 14 | 11 | 0.79 | 0.79 | $0.44 | 52 |
| d05-usePdfViewer | 14 | baseline r1 | 25 | 12 | 0.48 | 0.86 | $0.46 | 49 |
| d05-usePdfViewer | 14 | graph r0 | 14 | 12 | 0.86 | 0.86 | $0.33 | 49 |
| d05-usePdfViewer | 14 | graph r1 | 21 | 14 | 0.67 | 1.00 | $0.17 | 27 |
| d06-JuridicoCommonDialog | 15 | baseline r0 | 13 | 13 | 1.00 | 0.87 | $0.49 | 68 |
| d06-JuridicoCommonDialog | 15 | baseline r1 | 13 | 13 | 1.00 | 0.87 | $0.52 | 37 |
| d06-JuridicoCommonDialog | 15 | graph r0 | 15 | 15 | 1.00 | 1.00 | $0.17 | 27 |
| d06-JuridicoCommonDialog | 15 | graph r1 | 15 | 15 | 1.00 | 1.00 | $0.11 | 14 |
| d07-paginatedTableDisplayMessage | 13 | baseline r0 | 13 | 12 | 0.92 | 0.92 | $0.18 | 27 |
| d07-paginatedTableDisplayMessage | 13 | baseline r1 | 14 | 12 | 0.86 | 0.92 | $0.38 | 49 |
| d07-paginatedTableDisplayMessage | 13 | graph r0 | 13 | 13 | 1.00 | 1.00 | $0.22 | 29 |
| d07-paginatedTableDisplayMessage | 13 | graph r1 | 13 | 13 | 1.00 | 1.00 | $0.18 | 28 |
| d08-normalizeLiquidacaoListFilterFromUrl | 14 | baseline r0 | 14 | 14 | 1.00 | 1.00 | $0.21 | 28 |
| d08-normalizeLiquidacaoListFilterFromUrl | 14 | baseline r1 | 14 | 14 | 1.00 | 1.00 | $0.35 | 51 |
| d08-normalizeLiquidacaoListFilterFromUrl | 14 | graph r0 | 17 | 14 | 0.82 | 1.00 | $0.30 | 34 |
| d08-normalizeLiquidacaoListFilterFromUrl | 14 | graph r1 | 14 | 14 | 1.00 | 1.00 | $0.34 | 37 |
