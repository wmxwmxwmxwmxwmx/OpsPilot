# MySQL 慢查询排查

document_id: mysql/slow_query
version: 1.0
component: MySQL
updated: 2026-10-19

开发用 Runbook；无生产环境配置或真实故障数据。所有操作仅为建议，需人工授权。

## 常见症状
查询延迟上升，扫描行数显著增加。

## 检查步骤
在授权范围查看慢查询日志，比较 Query_time、Rows_examined 与 Rows_sent。
先用 EXPLAIN 查看执行计划，结合锁等待和索引选择分析。

## 可能原因
缺少适用索引、数据分布变化、锁等待或返回数据过多。

## 解决建议
在测试环境验证索引与查询改写，并比较执行计划和业务结果。

## 风险提示
EXPLAIN ANALYZE 会执行查询；启用日志、建索引或终止查询需要人工评估。

## 技术依据
https://dev.mysql.com/doc/refman/8.4/en/slow-query-log.html
