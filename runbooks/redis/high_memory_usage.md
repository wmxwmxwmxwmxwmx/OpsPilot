# Redis 内存使用升高

document_id: redis/high_memory_usage
version: 1.0
component: Redis
updated: 2026-10-19

开发用 Runbook；无生产环境配置或真实故障数据。所有操作仅为建议，需人工授权。

## 常见症状
used_memory 增长，可能伴随淘汰或写入失败。

## 检查步骤
检查 INFO memory 中 used_memory、used_memory_rss、maxmemory；比较历史趋势与键数量。
检查 maxmemory-policy 与 evicted_keys，抽样检查大键，避免全量 KEYS。

## 可能原因
数据增长、大键、碎片或容量配置不适用。

## 解决建议
按业务保留要求评估键大小和生命周期，先在隔离环境验证变更。

## 风险提示
maxmemory 不是进程 RSS 的硬上限；删除键或更改淘汰策略可能造成业务数据损失。

## 技术依据
https://redis.io/docs/latest/management/optimization/memory-optimization/
