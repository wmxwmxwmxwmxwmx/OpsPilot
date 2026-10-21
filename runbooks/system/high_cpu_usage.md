# Linux CPU 使用率升高

document_id: system/high_cpu_usage
version: 1.0
component: Linux
updated: 2026-10-19

开发用 Runbook；无生产环境配置或真实故障数据。所有操作仅为建议，需人工授权。

## 常见症状
请求变慢，CPU 持续忙碌或 load average 升高。

## 检查步骤
多次采样 /proc/stat 的增量，区分用户态、内核态与等待。
结合进程 CPU、可运行队列、CPU 数量和容器配额。

## 可能原因
计算密集、循环异常、内核开销或调度竞争。

## 解决建议
结合发布记录与性能剖析定位热点，在测试环境评估优化。

## 风险提示
Linux load average 也包含不可中断等待任务，不能等同 CPU 使用率；不要自动终止进程。

## 技术依据
https://www.kernel.org/doc/html/latest/filesystems/proc.html
