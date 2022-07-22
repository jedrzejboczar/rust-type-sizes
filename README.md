# rust-type-sizes

A script to analyze type sizes used in a Rust program.
It first compiles the code with `cargo +nightly rustc <args> -- -Zprint-type-sizes`
(requires nightly toolchain) and then generates a HTML report.
The report is organized into collapsible tree of types/fields/variants
with information about their size, alignment and offset (fields).
