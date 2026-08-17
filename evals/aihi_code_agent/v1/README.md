# `aihi_code_agent` benchmark v1

This version contains nine deterministic smoke-plus tasks covering bug fixing,
feature implementation, test repair, a security boundary, refactoring,
repository understanding, instruction following, interruption/resume and
Subagent planning. The manifest pins fixture hashes and hidden-style oracle
commands; `baseline.json` records the scripted reference executor result. This
baseline validates the runner/oracle chain and is explicitly not a real-model
capability score.
`nightly.config.example.toml` documents the required real-Provider, credential
environment and Docker/no-network settings; it is a template and is not
usable until its model and environment are supplied outside the repository.
