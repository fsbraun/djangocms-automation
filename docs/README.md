Docs shortcut
=============

This file is a small shortcut to the project README and contains quick instructions
for building the documentation locally.

Project README (source):

  ../README.md

Quick build instructions
------------------------

1. Create the docs virtual environment and install requirements:

   ```bash
   cd docs
   make install
   ```

2. Build the HTML documentation (uses the venv's Sphinx):

   ```bash
   cd docs
   make html
   ```

3. Open the built docs in your browser:

   ```bash
   open _build/html/index.html
   ```

If you want `README.md` content embedded into the docs pages, we can add a Sphinx
`rst` page that includes the Markdown file using `myst-parser` or by converting it
to reStructuredText. Tell me if you'd like that.
