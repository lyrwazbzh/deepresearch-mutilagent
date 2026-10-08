---
name: data-analysis
description: Inspect governed SQL or Dataset resources and produce bounded structured findings.
required_tools:
  - inspect_schema
  - sample_table
  - execute_readonly_sql
  - inspect_dataset
  - profile_dataset
  - query_dataset
---
# Data analysis

Confirm schema and metric semantics before querying. Use only read-only SQL or governed Dataset
operations. Return adopted SQL branch results or Dataset references; never create an
AnalysisRequest, chart, or arbitrary DML.
