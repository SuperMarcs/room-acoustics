# Third-party dependencies

Room Acoustics is licensed under the [MIT License](LICENSE). That license
applies to this project's original code and documentation; dependencies retain
their own licenses and copyright notices.

The application installs its dependencies separately through
[`requirements.txt`](requirements.txt). Their source code and binaries are not
included in this repository.

| Direct dependency | Declared version range | Main upstream license |
| --- | --- | --- |
| NumPy | `>=1.24,<3` | [BSD 3-Clause](https://numpy.org/doc/stable/license.html) |
| SciPy | `>=1.10,<2` | [BSD 3-Clause](https://github.com/scipy/scipy/blob/main/LICENSE.txt) |
| Matplotlib | `>=3.7,<4` | [Matplotlib license, based on the PSF license](https://matplotlib.org/stable/project/license.html) |
| pyroomacoustics | `>=0.10,<0.11` | [MIT](https://github.com/LCAV/pyroomacoustics/blob/master/LICENSE) |
| PySide6 / Qt for Python | `>=6.6,<7` | [LGPLv3 / GPL and commercial licensing options](https://www.qt.io/development/download-open-source) |

## PySide6 and Qt

The application uses PySide6's QtCore, QtGui, and QtWidgets modules. The
LGPLv3 licensing route allows the application's original code to remain under
MIT while the libraries retain their upstream licenses. See
[Qt's LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations).

If distributing an application bundle that includes PySide6 or Qt libraries,
include the required copyright and license notices, provide the corresponding
library source through a method permitted by the LGPL, and preserve users'
ability to replace or modify the LGPL libraries. Other Qt modules may have
different licensing terms.

## Scope

This is a summary of direct dependencies, not a complete inventory of their
transitive dependencies or bundled components. For any release that redistributes
third-party code, binaries, fonts, or other assets, retain the required upstream
notices and review the licenses shipped with the exact versions being distributed.
