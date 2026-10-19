# Statistics pipeline architecture

`statistics-pipeline.mmd` is the editable Mermaid source. The SVG is a scalable,
accessible export with native SVG text (`htmlLabels: false`); the PNG is the
README-friendly raster export. The diagram describes the local batch pipeline,
not a continuously available hosted service.

Solid arrows carry observations or published artifacts. Dashed arrows indicate
refresh control or a separately invoked consumer. The synthetic offline demo is
an alternative input and does not carry the real archive's counts. The research
consumer is not retrained automatically by the operations refresh.

`*` The dataset gate's default is 90% of prior card, bout and usable-paired-bout
counts, when a prior ready version exists. Nonempty checks always apply. An
explicitly reviewed `--min-retained-fraction` can change that threshold. Paired
round and nullable-field checks happen before this dataset gate. This gate
belongs to the scheduled refresh workflow. The offline demo instead checks its
own declared fixture expectations; the separate card-report and model commands
read the verified SQLite store directly.

MySQL schema initialization precedes its data transaction; schema DDL is not
part of the atomic load. The load, destination validation and active-pointer
switch share one InnoDB transaction. SQLite's local ready pointer activates
after optional MySQL publication; the two stores are not a distributed
transaction. Failed local activation is reconciled by replaying the same
warehouse version. See [the publication contract](../MYSQL_WAREHOUSE.md) and
[operations recovery](../PIPELINE_OPERATIONS.md).

The local schedule is configured for 09:00. A manual launchd kickstart verified
execution; it does not demonstrate a future scheduled run or operation while
the Mac is off. Failure notifications depend on macOS notification settings.

## Render

From the repository root, with Node.js 22.13+ and an installed Chrome browser:

```sh
sh scripts/render_architecture.sh
```

The script pins `@mermaid-js/mermaid-cli@12.0.0`, keeps the npm cache outside the
repository and disables Puppeteer's browser download. The initial package fetch
needs network access. It never modifies project runtime dependencies. Override
`PUPPETEER_EXECUTABLE_PATH` on another machine. The committed config selects the
existing macOS Chrome executable by default.

Mermaid references: [CLI](https://github.com/mermaid-js/mermaid-cli) and
[flowchart syntax](https://mermaid.js.org/syntax/flowchart.html).
