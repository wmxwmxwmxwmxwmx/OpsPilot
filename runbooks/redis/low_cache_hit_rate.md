# Redis 缓存命中率下降

document_id: redis/low_cache_hit_rate
version: 1.0
component: Redis
updated: 2026-10-19

开发用 Runbook；无生产环境配置或真实故障数据。所有操作仅为建议，需人工授权。

## 常见症状
缓存 miss 增多，回源请求增加。

## 检查步骤
在相同时间窗口读取 INFO stats 的 keyspace_hits 与 keyspace_misses 增量；分母为零时不计算命中率。
对照 expired_keys、evicted_keys 增量，检查发布时点、TTL 分布和键名变更。

## 可能原因
热点过期、内存淘汰、冷启动或访问模式变化。

## 解决建议
先区分过期与淘汰，再评估 TTL 分散、容量和预热；本手册没有执行任何修改。

## 风险提示
禁止以 FLUSHALL 修复命中率；变更 TTL 或淘汰策略必须先评估一致性和容量。

## 技术依据
https://redis.io/docs/latest/develop/reference/eviction/
