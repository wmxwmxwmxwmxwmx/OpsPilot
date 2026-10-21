# MySQL 连接耗尽

document_id: mysql/connection_exhaustion
version: 1.0
component: MySQL
updated: 2026-10-19

开发用 Runbook；无生产环境配置或真实故障数据。所有操作仅为建议，需人工授权。

## 常见症状
客户端出现 Too many connections。

## 检查步骤
比较 Threads_connected、Threads_running 与 max_connections。
检查连接池上限、连接归还和长事务；对照应用实例数量。

## 可能原因
连接泄漏、慢查询占用、连接池总容量超出数据库能力。

## 解决建议
先定位连接来源和等待原因，验证连接池配置及超时。

## 风险提示
盲目提高 max_connections 可能增加内存压力；不得自动 KILL 生产会话。

## 技术依据
https://dev.mysql.com/doc/refman/8.4/en/too-many-connections.html
