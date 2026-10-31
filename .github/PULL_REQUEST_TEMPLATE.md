Closes #<issue 编号>

<!-- ⚠️ 上面这行是 GitHub 的「关闭关键字」，必须：① 留在 HTML 注释之外；② 与 #编号 写在同一行；
     ③ 用 close/closes/fix/fixes/resolve/resolves 之一。把编号写进标题或普通句子不会自动收单。
     此前 T08–T40 的 32 个 issue 就是因为漏了关键字而一直 OPEN。合并后请核实：
     gh issue view <编号> --json state  → 期望 CLOSED -->

## 改了什么

<!-- 简述；一份 PR 只做一件事 -->

## 验收命令与实际输出

<!-- 必填：粘贴真实执行结果，不写「已验证」了事 -->

```bash
# 命令
# 实际输出
```

## 自查清单

- [ ] **未触碰 NG-2/NG-3 保留实现**（langgraph 路径 / V1 三件套；如改动请说明理由）
- [ ] **未引入任何密钥**（示例值用 test-only-* 风格）
- [ ] commit message 为英文前缀 + 中文正文
- [ ] 可执行代码改动已跑 `py_compile` / 受影响测试
