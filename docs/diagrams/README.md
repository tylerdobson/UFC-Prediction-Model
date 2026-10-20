# MySQL architecture diagram

The editable Mermaid source produces the SVG and README PNG. Solid arrows carry data; dashed arrows show control, migration, or a separate invocation. MySQL is the runtime database for operating records, canonical statistics, research, rankings, evaluations and the forecast ledger. Raw captures and report exports remain files.

New statistics versions are normalized directly in MySQL. After quality checks, the statistics alias and its publication receipt commit in one transaction. Warehouse analytical-view publication has its own validated InnoDB transaction; replay reconciles an interrupted refresh between these publication steps. Dashboard reads pin each opened immutable dataset. Schema DDL occurs before publication transactions.

Synthetic demo datasets have a separate role and namespace. The demo cannot activate the real statistics alias or warehouse view. Legacy SQLite files are retained migration inputs and explicit regression fixtures; normal operation does not stage through them or fall back to them.

Model evaluation is a separate job. Current rankings are descriptive and do not become historical training features. Local scheduling depends on the Mac being awake and logged in. See the [runtime contract](../MYSQL_RUNTIME.md).

To render, from the project root:

```sh
sh scripts/render_architecture.sh
```

The script pins Mermaid CLI 12.0.0 and uses local Chrome. Override `PUPPETEER_EXECUTABLE_PATH` when needed. The initial npm fetch needs network access.
