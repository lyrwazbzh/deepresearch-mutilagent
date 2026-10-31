/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

/**
 * 全站唯一的「浏览器本地存储」访问点（T37 方案 A）。
 *
 * **为什么收敛到一处**：登录态（JWT）就存在这里，而本地存储对 XSS 毫无防护 ——
 * 任意一次 XSS 都能把它读走。收敛到单一模块后，将来若要换成 httpOnly Cookie、
 * 加内存缓存或做加密，只需要改这一个文件，而不必满仓库找调用点。
 * 因此除本文件外，`src/` 下的**源码**不应再直接出现底层存储 API
 * （测试文件为直接控制全局状态、避免「用被测代码验证自己」而例外）。
 *
 * 只暴露**字符串原语**，不做 JSON 封装：各调用方对「坏数据」的策略并不相同
 * （有的静默丢弃、有的删键后重试、有的保留默认值），统一的 JSON 包装会把它们的
 * 语义抹平，反而更容易引入行为回归。
 */

/** 读取字符串值；键不存在时返回 null。 */
export function readItem(key: string): string | null {
  return window.localStorage.getItem(key)
}

/** 写入字符串值。 */
export function writeItem(key: string, value: string): void {
  window.localStorage.setItem(key, value)
}

/** 删除键。 */
export function removeItem(key: string): void {
  window.localStorage.removeItem(key)
}

/** 列出全部键（供 valtio-persist 的存储引擎适配使用）。 */
export function allKeys(): string[] {
  return Object.keys(window.localStorage)
}
