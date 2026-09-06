# Changelog

## [0.3.0](https://github.com/neiro-data/analyze-claude-sessions/compare/v0.2.0...v0.3.0) (2026-09-06)


### Features

* context-window and pacing metrics for Grafana dashboards ([4fae4a5](https://github.com/neiro-data/analyze-claude-sessions/commit/4fae4a5d788ac16dae8fd8c33fe692d45a86d0d3))

## [0.2.0](https://github.com/neiro-data/analyze-claude-sessions/compare/v0.1.0...v0.2.0) (2026-09-06)


### Features

* add release-please automated versioning ([1a74201](https://github.com/neiro-data/analyze-claude-sessions/commit/1a74201c7b3b8ff6ad4a98cf79a7b1fe00a78568))
* add release-please automated versioning ([9fcf6a7](https://github.com/neiro-data/analyze-claude-sessions/commit/9fcf6a7f8b9181b8cbc870ad668a3d4db0762e7f))
* **cost,duckdb:** model pricing + cost columns, DuckDB over parquet ([065d0cf](https://github.com/neiro-data/analyze-claude-sessions/commit/065d0cfa03bb7d6a154ba49744e5df074411bca5))
* **cost,duckdb:** model pricing + cost columns, DuckDB over parquet ([66cd8a9](https://github.com/neiro-data/analyze-claude-sessions/commit/66cd8a91bc39e3c34cbc06d4013f131aaf8596b8))
* CSV-sourced model pricing, refresh CLI, cost views, Grafana panels ([13cdaa7](https://github.com/neiro-data/analyze-claude-sessions/commit/13cdaa73cd16f5cbd88e50213ee7286838012a10))
* **dashboard:** add Grafana usage overview + session deep-dive ([684dd79](https://github.com/neiro-data/analyze-claude-sessions/commit/684dd79009df75f8bf1539b92658c2e74d5f63d6))
* **dashboard:** add Grafana usage overview + session deep-dive ([ad30764](https://github.com/neiro-data/analyze-claude-sessions/commit/ad3076441a2cbeaac887a4d82b680994c283444e))
* **dashboard:** combined session family dashboard with scope toggle ([92c86f5](https://github.com/neiro-data/analyze-claude-sessions/commit/92c86f58cf6f6adab00dac32a022efe55c300f46))
* **dashboard:** combined session family dashboard with scope toggle ([d1667f4](https://github.com/neiro-data/analyze-claude-sessions/commit/d1667f4215f11a1dd2763e022190a31d1c3b4126))
* **dashboard:** finer time bins + quieter ingest skip output ([98a17ae](https://github.com/neiro-data/analyze-claude-sessions/commit/98a17aefcce43867e30cce2af18505659ceb6849))
* **dashboard:** finer time bins between 1h-2d; hide unchanged-skip lines in ingest ([4c1fa02](https://github.com/neiro-data/analyze-claude-sessions/commit/4c1fa02a2ef0c893a0074fa74bea954fa0230d7b))
* **dashboard:** overview + session-detail improvements ([7a07ed3](https://github.com/neiro-data/analyze-claude-sessions/commit/7a07ed380ed19845bccefb54b8c9897281b3f6bf))
* **dashboard:** overview + session-detail improvements ([8315f43](https://github.com/neiro-data/analyze-claude-sessions/commit/8315f43a757f07a623cc2fc967a26808c6cb3e4f))
* **dashboard:** polish group-sessions and lineage dashboards ([0bf834d](https://github.com/neiro-data/analyze-claude-sessions/commit/0bf834ddab9652dde77566edc16ec3ad8d87b944))
* **dashboard:** polish group-sessions and lineage dashboards ([6739fe5](https://github.com/neiro-data/analyze-claude-sessions/commit/6739fe5029ef495b31f71c1b15f2cc857b3abe9d))
* **grafana:** add API calls table to Session Detail dashboard ([0c71d05](https://github.com/neiro-data/analyze-claude-sessions/commit/0c71d05845ae8bc3ed0d621c3e825f385eb3d969))
* **grafana:** color Call timeline by agent instance, add prompt_preview ([6d85494](https://github.com/neiro-data/analyze-claude-sessions/commit/6d85494c253bfbf8964859cab1137ed4254c6453))
* **grafana:** Session Detail API calls table + agent-colored Call timeline ([02ed09c](https://github.com/neiro-data/analyze-claude-sessions/commit/02ed09c5b742870eaf9926bdc4465551d4df541e))
* **ingest:** reproduce Claude Code's /stats non-deduplicated token totals ([06ea115](https://github.com/neiro-data/analyze-claude-sessions/commit/06ea115aab59eeba27a681b1e7d1a8b72ff8d93e))
* **ingest:** reproduce Claude Code's /stats non-deduplicated token totals ([a21fed6](https://github.com/neiro-data/analyze-claude-sessions/commit/a21fed6f5f74ca3dd77c70caf886599f5d482218))
* **ingest:** support new per-call CSV format with agent/depth columns ([8fe1624](https://github.com/neiro-data/analyze-claude-sessions/commit/8fe1624811c73fd7b0ca57199127c1287dd3f6ac))
* **ingest:** support new per-call CSV format with agent/depth columns ([96cc8e4](https://github.com/neiro-data/analyze-claude-sessions/commit/96cc8e444368ad3eb1b7a53968f418f1261bf576))
* **ingest:** symlink dedup, incremental tail ingest, launchd scheduling ([f7dbdea](https://github.com/neiro-data/analyze-claude-sessions/commit/f7dbdeaf777462b6f01afd68c114c0211e88b5ac))
* **ingest:** symlink dedup, incremental tail ingest, launchd scheduling ([7d5e791](https://github.com/neiro-data/analyze-claude-sessions/commit/7d5e791bc3788c1669f5ba13c4f36727e265b466))
* **lineage:** track parent/child session spawn chains ([c3b824f](https://github.com/neiro-data/analyze-claude-sessions/commit/c3b824f97c6228c301da70524314e026a956bae8))
* **lineage:** track parent/child session spawn chains ([c4d3a16](https://github.com/neiro-data/analyze-claude-sessions/commit/c4d3a1653aa5e9718d4d61a162934dfa0ed9b76c))
* **nlp:** cache corpus embeddings for CatalogRetriever ([2cb9c43](https://github.com/neiro-data/analyze-claude-sessions/commit/2cb9c43f71a0ea96b634e3dc48ba78a80aefae5c))
* **nlp:** cache corpus embeddings for CatalogRetriever ([8bf1c7e](https://github.com/neiro-data/analyze-claude-sessions/commit/8bf1c7e31461fd6485da1b5271caaddf4f8279f6))
* **nlp:** offline RAG catalog + `acp ask` natural-language queries ([7e79319](https://github.com/neiro-data/analyze-claude-sessions/commit/7e793194c4ab022fcf01fd83e668fc4ba76d25e1))
* **nlp:** offline RAG catalog + `acp ask` natural-language queries ([005a6ef](https://github.com/neiro-data/analyze-claude-sessions/commit/005a6efea6117df31051295b823eaeaaff943f28))
* **nlp:** session-scoped `acp ask` queries via session_id=&lt;id&gt; ([a91447a](https://github.com/neiro-data/analyze-claude-sessions/commit/a91447a6434504b0b6454a5252583e6f76101153))
* **nlp:** session-scoped `acp ask` queries via session_id=&lt;id&gt; ([2b62daf](https://github.com/neiro-data/analyze-claude-sessions/commit/2b62dafc6d4e8445b51cc72c4b285ebf1dbd6799))
* **nlp:** slot-aware matching for acp ask + retrieval eval harness ([a31b291](https://github.com/neiro-data/analyze-claude-sessions/commit/a31b29125345fac4e692ff8eeb3c4ab658ed7b4d))
* **nlp:** slot-aware matching for acp ask + retrieval eval harness ([b17abe0](https://github.com/neiro-data/analyze-claude-sessions/commit/b17abe090a93bf615a07c55338ace62e0fb3ac3a))
* **ops:** add data/csv retention prune script ([48c8440](https://github.com/neiro-data/analyze-claude-sessions/commit/48c84402950a5ca45d00c54e4ad912094e98ed8e))
* **ops:** add DB + parquet backup script ([be83b8f](https://github.com/neiro-data/analyze-claude-sessions/commit/be83b8f54d65c5e9d3036d63dc28bf5c2910624e))
* **ops:** add ingest wrapper + README Quick guide ([4cfc9cc](https://github.com/neiro-data/analyze-claude-sessions/commit/4cfc9ccbcec45737d20fe9cb3419236e2bcace8b))
* **ops:** data/csv retention prune script ([6d28c4c](https://github.com/neiro-data/analyze-claude-sessions/commit/6d28c4cfcb7a1acd9bbcef1f44ee2aed08528f9d))
* **ops:** DB + parquet backup script ([f41476f](https://github.com/neiro-data/analyze-claude-sessions/commit/f41476fd1c3c84a2de42480b636744a68f976ab7))
* **ops:** ingest wrapper + README Quick guide ([4991ee3](https://github.com/neiro-data/analyze-claude-sessions/commit/4991ee3c67e411ed50e19d096707a3745b2992cc))


### Bug Fixes

* **cli:** lazy-import nlp so optional numpy group doesn't break every command ([8d2d776](https://github.com/neiro-data/analyze-claude-sessions/commit/8d2d7762c88cdce82e1af1a3d967372723cf1ca5))
* **cli:** lazy-import nlp so optional numpy group doesn't break every command ([ac356c9](https://github.com/neiro-data/analyze-claude-sessions/commit/ac356c92c74f5c3580b8d20306097a89cef97b20))
* **dashboard:** adaptive time-bucket granularity for overview panels ([b4e451e](https://github.com/neiro-data/analyze-claude-sessions/commit/b4e451ecccb77af1ae67d0a0d8ada04f1af74387))
* **dashboard:** adaptive time-bucket granularity for overview panels ([43352f5](https://github.com/neiro-data/analyze-claude-sessions/commit/43352f51b349787a714f9fcf50aa41f1f5e62141))
* **dashboard:** correct session-detail axis labels and picker ([8b02029](https://github.com/neiro-data/analyze-claude-sessions/commit/8b02029152d26961c84c82d08eda2522bfa6a700))
* **dashboard:** lineage session_id filter, model(s) list, iteration tick spacing ([d53d3bb](https://github.com/neiro-data/analyze-claude-sessions/commit/d53d3bbc60ee1718caa7cd3214145e13c21f9f06))
* **dashboard:** lineage session_id filter, model(s) list, iteration tick spacing ([108e6ba](https://github.com/neiro-data/analyze-claude-sessions/commit/108e6baef84205bf97cb349f96883bbff6b3e877))
* **dashboard:** lineage session_id filter, model(s) list, iteration tick spacing ([3148021](https://github.com/neiro-data/analyze-claude-sessions/commit/3148021db6f504300556a3735f5d310c7838c94e))
* **dashboard:** session-detail axis labels and session picker ([48d6ec2](https://github.com/neiro-data/analyze-claude-sessions/commit/48d6ec207200fa3b87f88a7c98ebe5db5cce62fe))
* **dashboard:** session-detail panel defects + stacked cache view ([340f558](https://github.com/neiro-data/analyze-claude-sessions/commit/340f558b16352135ae9f1c224ff2b99b6f9c6cec))
* **dashboard:** session-detail panel defects + stacked cache view ([c051fa5](https://github.com/neiro-data/analyze-claude-sessions/commit/c051fa59abb2c754702f2c61198a46f463c58ffa))
* **grafana:** repair session_id filter and drop project_dir column ([e5b13da](https://github.com/neiro-data/analyze-claude-sessions/commit/e5b13dace1f8a275253f71a51e0af62198e35662))
* **grafana:** repair session_id filter and drop project_dir column ([1a8abc4](https://github.com/neiro-data/analyze-claude-sessions/commit/1a8abc4ed2e216f043df6881fa7be27b0c77a3f0))
* **ingest:** suppress no-API-call lines unless --verbose ([88e9193](https://github.com/neiro-data/analyze-claude-sessions/commit/88e91939aec5731cda2c4b5b79d9b931d20296b8))
* **ingest:** suppress no-API-call lines unless --verbose ([c8db2ec](https://github.com/neiro-data/analyze-claude-sessions/commit/c8db2eceba4ad88d7a7e5f705c4a9604962d0d21))
* **nlp:** enforce offline HF Hub load for acp ask ([e613a02](https://github.com/neiro-data/analyze-claude-sessions/commit/e613a02fe0ec2143aa0de0e0900dc510f6c52f22))
* **nlp:** enforce offline HF Hub load for acp ask ([acd8f46](https://github.com/neiro-data/analyze-claude-sessions/commit/acd8f4672c9153cf5f03fb476abbb5c0c32cef55))
* **nlp:** fix four slot-extraction defects ([181cb23](https://github.com/neiro-data/analyze-claude-sessions/commit/181cb2347719b251d66f71d71da3e049390ef347))
* **nlp:** fix four slot-extraction defects ([c223f7a](https://github.com/neiro-data/analyze-claude-sessions/commit/c223f7ace3fc4ff9831b1cfb4d42db0fe5bf0675))
* **nlp:** replace regex SQL guard with pglast parse-tree check ([dae9e64](https://github.com/neiro-data/analyze-claude-sessions/commit/dae9e64628920a2551eea2485b8218bcea122d73))
* **nlp:** replace regex SQL guard with pglast parse-tree check ([2ab478a](https://github.com/neiro-data/analyze-claude-sessions/commit/2ab478acd4a1f6a837f70f821bbb156ad1a3249d))


### Reverts

* **grafana:** drop prompt_preview feature from branch ([be86e51](https://github.com/neiro-data/analyze-claude-sessions/commit/be86e51c6b22bc3c27c0a4f6a60b6b356adc50f5))


### Documentation

* **handoff:** review HANDOFF against current code ([d43d72c](https://github.com/neiro-data/analyze-claude-sessions/commit/d43d72cda36347f6806821a7354ca1d2c459bf44))
* **handoff:** review HANDOFF against current code ([cbe6803](https://github.com/neiro-data/analyze-claude-sessions/commit/cbe680376ace7480b1419e7bd9927ad8d6020ba7))
* log add-agent-columns-and-sessions-tables interaction ([26e2e57](https://github.com/neiro-data/analyze-claude-sessions/commit/26e2e573a7eb990acd9d52867eda02e1bcb1ec51))
* **nlp:** note pglast-based guard in README ([799cbe0](https://github.com/neiro-data/analyze-claude-sessions/commit/799cbe0e36bcce735e9de3a137cb272e9bb514cb))
* **readme:** document uv run acp ask; name agents + add reuse in cache report ([5b59c54](https://github.com/neiro-data/analyze-claude-sessions/commit/5b59c549fdacc0e3a02d1b93b012330a795a9c59))
* reorganize README, resolve sql/ migration number collision ([96dc156](https://github.com/neiro-data/analyze-claude-sessions/commit/96dc1561331eb8650b2268989d870c12f36c4dea))
* reorganize README, resolve sql/ migration number collision ([682a9d5](https://github.com/neiro-data/analyze-claude-sessions/commit/682a9d57dd406de7c928f101e7756365335f1eef))
* uv run acp ask how-to + named agents & reuse in cache report ([5544f8d](https://github.com/neiro-data/analyze-claude-sessions/commit/5544f8d30f4737bcfedfa6f8f80735ea5fa9d519))
