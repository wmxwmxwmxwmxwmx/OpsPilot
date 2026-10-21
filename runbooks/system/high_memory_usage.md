# Linux 内存使用升高

document_id: system/high_memory_usage
version: 1.0
component: Linux
updated: 2026-10-19

开发用 Runbook；无生产环境配置或真实故障数据。所有操作仅为建议，需人工授权。

## 常见症状
可用内存下降，交换活动或 OOM 事件增加。

## 检查步骤
查看 /proc/meminfo 的 MemAvailable、缓存和交换统计。
比较进程 RSS 趋势及 cgroup 内存限制，核查 OOM 记录。

## 可能原因
进程工作集增长、泄漏、缓存增长或容器限制过低。

## 解决建议
结合持续采样定位增长源，确认内存需求后再评估容量。

## 风险提示
free 很低不必然代表内存不足；不要自动 drop_caches、重启或终止进程。

## 技术依据
https://www.kernel.org/doc/html/latest/filesystems/proc.html
