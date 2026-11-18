# Contributing to CodeAtlas

Contributions are welcome! Here is how to get set up locally:

1. Fork and clone the repository.
2. Install Python dev dependencies:
   ```bash
   pip install -e ".[dev]"
   ```
3. Run the test suite:
   ```bash
   pytest
   ```
4. If you modify the Svelte UI in `frontend/`, rebuild the static bundle before committing:
   ```bash
   cd frontend
   npm install
   npm run build
   ```

Please include unit tests for any new language analyzers or graph resolution rules.
