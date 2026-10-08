# DeepResearch-mutilagent：ReportAst 与 Citation

## 0. 定位

```text
LeadWriter
  → ReportAst candidate
  → ValidateReport（Report Schema Validation + CitationValidator）
       ├─ invalid + revision budget → Revise → 新 ReportAst candidate → ValidateReport
       ├─ invalid + exhausted → GraphFailure
       └─ valid → report.json + report.md → report.draft
  → Finalize → report.final
```

本文定义 ReportAst、Claim/Citation、引用验证、报告版本和统一渲染合同。证据内容统一从 `evidence_pool` 解析；Document、Web、SQL 和 Compute 的内容承载方式见 [`state.md`](../01_architecture/state.md) 与 [`memory.md`](memory.md)。

---

## 1. Report Version

每个报告版本在当前 Task Workspace 中包含：

```text
reports/versions/{report_version}/
├─ report.json       # 权威 ReportAst
└─ report.md         # 从同版本 AST 确定性生成
```

发布流程：

```text
Schema-valid ReportAst
  → CitationValidator
  → valid=true
  → Renderer 生成 report.md
  → report.json 与 report.md 均成功写入并通过 hash/size 校验
  → 提交 report.draft
```

```text
report.draft:
  version = report_version
  workspace_path = reports/versions/{report_version}/report.json
  content_hash = sha256(report.json)
```

Finalize 执行：

```text
report.final = report.draft
```

Citation Validation 是可确定性重算的运行结果，不生成独立验证文件。

---

## 2. ReportAst

### 2.1 顶层结构

```yaml
ReportAst:
  report_version: int
  title: string
  sections: Section[]
  citations: Citation[]
```

```yaml
Section:
  section_id: string
  heading: string
  level: int
  blocks: Block[]

Block:
  ParagraphBlock | ListBlock | TableBlock | ImageBlock
```

### 2.2 Paragraph 与 List

```yaml
ParagraphBlock:
  type: paragraph
  segments: Segment[]

ListBlock:
  type: list
  ordered: bool
  items:
    - segments: Segment[]

Segment:
  TextSegment | ClaimSegment

TextSegment:
  type: text
  text: string

ClaimSegment:
  type: claim
  claim_id: string
  text: string
  citation_ids: string[]
```

`claim_id` 在当前 ReportAst 内唯一。Final Report 中需要外部证据支持的 Claim 至少绑定一个 Citation。

### 2.3 Table

```yaml
TableBlock:
  type: table
  caption: string?
  columns: string[]
  rows: string[][]
  citation_ids: string[]
```

`columns` 非空且列名唯一，每行长度与列数一致。Report Table 只承载报告实际展示的有界数据；完整 SQL 长结果保存在 Workspace。表格的数据依据由 `citation_ids` 指向 Evidence。

### 2.4 Image

```yaml
ImageBlock:
  type: image
  workspace_path: string
  content_hash: string
  alt_text: string
  caption: string?
  citation_ids: string[]
```

图表、Sandbox 结果和其他生成图片都存放在当前 Task Workspace。数据型图片的 `citation_ids` 指向生成它所依据的 Document、Web 或 SQL Evidence；Validator 通过 `workspace_files.producer_tool_call_id → compute/{tool_call_id}/manifest.json` 校验这些 Citation 覆盖 manifest 中的输入 Evidence。纯展示图片可以使用空列表。

---

## 3. Citation

### 3.1 Schema

```yaml
Citation:
  citation_id: string
  evidence_id: string
  locator: string?
```

`citation_id` 在报告内唯一。它把一个 Claim、Table 或 Image 绑定到一项不可变 Evidence；`locator` 是简短显示提示，例如页码、章节或行范围。

Citation 不复制 Evidence 的来源字段：

```text
Citation.evidence_id
  → evidence_pool[evidence_id]
  → data / doc_id+chunk_id / workspace_path
```

### 3.2 内容位置与来源追溯

```text
Document 内容
  → doc_id + chunk_id + content_hash

内联短结果
  → evidence_id + data + content_hash

Workspace 长内容
  → workspace_path + content_hash

Web 来源追溯
  → source_uri + retrieved_at
```

`source_uri` 表示网页出处，`retrieved_at` 表示本次获取时间；两者不承担不可变内容存储。Web 短结果的 hash 校验 `Evidence.data`，Web 长正文的 hash 校验 Workspace 文件。

SQL 包含外部数据库查询与 XLSX / CSV / Parquet 经 DuckDB 执行的查询；Dataset 不形成独立 Citation 来源。Compute 结果始终使用 Workspace 文件，其图表同时引用上游事实 Evidence。

---

## 4. Markdown Citation

Markdown 是 Renderer 从 ReportAst 生成的投影：

```markdown
市场规模同比增长 12%。[^cite-cit_001][^cite-cit_002]

[^cite-cit_001]: 来源标题，§3.2
[^cite-cit_002]: 另一来源标题，p. 14
```

相邻 footnote token 表示多个 Citation 共同支持同一内容。显示标题、链接和 locator 由 Renderer 根据 Citation 与 Evidence 临时解析。

---

## 5. CitationValidator

### 5.1 输入与输出

```yaml
CitationValidationInput:
  report_ast: ReportAst
  evidence_pool: Evidence[]

CitationValidationResult:
  valid: bool
  issues: CitationValidationIssue[]

CitationValidationIssue:
  severity: error | warning
  code: string
  message: string
  claim_id: string?
  citation_id: string?
  evidence_id: string?
```

Validator 和 Renderer 可以在进程内使用带展示标题、链接与 locator 的临时解析结构。该结果不写入 ResearchState、ReportAst、数据库或独立文件。

### 5.2 确定性检查

Validator 检查：

1. ReportAst 联合类型、Section level 和字段不变量。
2. `claim_id` 与 `citation_id` 在报告内唯一。
3. 需要证据支持的 Claim、Table 和数据型 Image 已绑定 Citation。
4. 每个被引用的 `citation_id` 存在；未使用 Citation 产生 warning。
5. 每个 `evidence_id` 存在于当前 Task 的 `evidence_pool`。
6. Document Evidence 的 `doc_id / chunk_id / content_hash` 可解析且内容匹配。
7. Workspace Evidence 的 `workspace_path / content_hash` 属于当前 Task 且内容匹配。
8. Web / SQL 短 Evidence 的 `data` 规范化 hash 与 `content_hash` 匹配。
9. 当前身份仍可读取对应来源。

错误码：

```text
invalid_report_structure
duplicate_claim_id
duplicate_citation_id
missing_citation
citation_not_found
evidence_not_found
source_not_found
content_hash_mismatch
permission_denied
```

运行依赖不可用作为节点执行错误处理，不混入引用内容错误。

---

## 6. Draft、Revise 与 Finalize

```text
LeadWriter 输出 ReportAst candidate
  → ValidateReport
       → Report Schema Validation
       → CitationValidator
       ├─ invalid + revision budget
       │    → Revise 独立 Graph Node
       │    → 根据 issues 生成新 ReportAst candidate
       │    → ValidateReport
       ├─ invalid + exhausted
       │    → GraphFailure
       └─ valid
            → 发布 report.json + report.md
            → State 提交 report.draft
            → Finalize
```

```text
validation.valid == false
  → Graph 调度 Revise NodeRun
  → 生成新 candidate 后重新进入 ValidateReport
  → revision budget 耗尽时 GraphFailure

schema.valid && citation_validation.valid
  → 发布 Draft
  → Finalize → report.final = report.draft
```

CriticMaster 已在 Writer 之前完成研究语义审核。报告阶段只运行确定性的 Schema/Citation Validation；`Revise` 使用 LeadWriter 的修订能力，但作为独立 Graph Node 拥有独立 NodeRun 与恢复边界。需要重试或恢复时依据候选 ReportAst 与 Evidence 重新计算，不增加 ResearchState 字段，也不把无效候选提交为 Draft。

---

## 7. Evidence 生命周期

报告引用的 Evidence 在当前 Task 生命周期内保持不可变并持续保留：

```text
同一逻辑 ToolResult 重放
  → 复用原 Evidence

来源或内容变化
  → 生成新的 Evidence

不同来源内容相同
  → 分别保留独立 Evidence
```

MergeEvidence 负责多分支合流、schema 归一和重放复用，不按 `content_hash` 跨来源合并。一个 Claim 可以绑定多个 Citation，以表达多个独立来源的共同支持。

---

## 8. 多格式渲染

Phase 4 核心报告主链只要求发布权威 `report.json` 与确定性 `report.md`，并完成 `report.draft → report.final`。HTML、DOCX、PDF 与 Share 保留统一 Renderer 合同，由后续 Export / Share 阶段实现，不扩大 Phase 4 的交付范围。

```text
ReportAst + evidence_pool
  → 解析 Citation 展示信息
  ├─ Markdown：命名 footnote
  ├─ HTML：sup anchor + source cards
  ├─ DOCX：统一内容投影
  ├─ PDF：HTML / Print CSS
  └─ Share：冻结允许公开的 source cards 与附属资源
```

渲染时产生的 Citation 展示结构只在当前渲染过程存在。已发布的 `report.md` 保留该版本已经解析出的脚注文本。

---

## 9. 可观测与测试

### 9.1 可观测

```text
Trace span attributes：
  task_id
  report_version

失败事件：
  claim_id?
  citation_id?
  evidence_id?
  validation.code

Metrics：
  report_validation_total{result}
  report_validation_issue_total{code}
  report_render_total{result, format}
```

### 9.2 测试

```text
□ Paragraph/List 的 Text/Claim segment 可稳定校验
□ Table 的 columns/rows 与 citation_ids 可稳定校验
□ Image 读取当前 Task Workspace 文件并校验 content_hash
□ Citation 通过 evidence_id 解析 Document、inline data 与 Workspace
□ 不同来源的相同内容形成独立 Evidence，并可同时支持一个 Claim
□ 相同逻辑结果重放不重复追加 Evidence
□ Web source_uri/retrieved_at 只承担来源追溯
□ Phase 4 的 report.json/report.md 内容与 Citation 顺序一致
□ 后续 HTML/DOCX/PDF/Share Renderer 复用同一 ReportAst 与 Citation 顺序合同
```

---

## 10. 验收标准

```text
□ ReportAst 顶层包含唯一 citations 集合
□ Citation 只保存 citation_id、evidence_id 与可选 locator
□ 一个 Report Version 只有权威 report.json 与确定性 report.md
□ Claim/Table/Image → Citation → Evidence → 内容位置可完整追踪
□ Dataset 归入 SQL，Compute 产物全部位于 Workspace
□ Compute 图表引用上游事实 Evidence
□ Validation 可确定性重算，不依赖持久化验证快照
□ Phase 4 核心交付 report.json、report.md、report.draft 与 report.final
□ HTML、DOCX、PDF、Share 属于后续 Renderer / Export / Share 实现范围
```
