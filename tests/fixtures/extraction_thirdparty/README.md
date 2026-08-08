# Out-of-tree extraction adapters, for proving discovery

Three tiny packages that exist to be *installed* rather than imported. Each is
copied into a `tmp_path`, given a generated `.dist-info` with a real
`entry_points.txt`, and put on `sys.path` for the length of one test — so
discovery is exercised through `importlib.metadata` the way a real
`pip install` would present it.

They live here as ordinary source rather than as strings inside the test so
`ruff check .` and the file-size gate see them. A fixture nobody lints is a
fixture that rots.

| Package | Selector | What it is for |
|---|---|---|
| `acme_extraction/` | `acme` | The happy path, and the worked example. Passes the shipped contract suite. |
| `acme_mismatch/` | `mismatch` | Declares a `provider_id` that disagrees with its selector. The smoke check must refuse it. |
| `acme_broken/` | `broken` | Raises at import from valid Python. The failed-import path must be fatal and say who. |

`acme_broken` raises from a module-level `raise`, never a syntax error. A file
that will not parse would fail `ruff check .` and the file-size gate over this
directory, which would mean fixing the gates to accommodate a fixture — exactly
backwards. Valid Python that refuses to import is the honest version of the
failure anyway: that is what a real dependency does when its own import-time
setup fails.
