# Contributing to MCP Foxxy Bridge

Thank you for your interest in contributing to MCP Foxxy Bridge! This guide
will help you get started with development and contribution workflows.

## Development Setup

### Prerequisites

- **Python 3.10+** - Required for running the bridge
- **Node.js 18+** - Required for MCP servers that use npm
- **UV** - Python package manager and tool runner
- **Git** - Version control

### Getting Started

1. **Clone the repository**

   ```bash
   git clone https://github.com/billyjbryant/mcp-foxxy-bridge
   cd mcp-foxxy-bridge
   ```

2. **Install dependencies**

   ```bash
   uv sync
   ```

3. **Run the bridge in development mode**

   ```bash
   uv run mcp-foxxy-bridge --bridge-config bridge_config_example.json
   ```

4. **Test the installation**

   ```bash
   # Check status endpoint
   curl http://localhost:8080/status
   ```

### Development Commands

```bash
# Run the bridge as a module
uv run -m mcp_foxxy_bridge

# Run the bridge as a package
uv run mcp-foxxy-bridge

# Run tests
pytest

# Run tests with coverage
coverage run -m pytest
coverage report

# Type checking
mypy src/

# Linting
ruff check

# Code formatting
ruff format

# Install from source
uv tool install .
```

## Project Structure

```
mcp-foxxy-bridge/
├── src/mcp_foxxy_bridge/      # Main source code
│   ├── __main__.py            # CLI entry point
│   ├── mcp_server.py          # HTTP/SSE server
│   ├── bridge_server.py       # MCP protocol bridge
│   ├── server_manager.py      # MCP server connections
│   └── config_loader.py       # Configuration parsing
├── tests/                     # Test files
├── docs/                      # Documentation
├── bridge_config_example.json # Example configuration
├── pyproject.toml            # Project configuration
└── README.md                 # Main project documentation
```

## Code Style and Standards

### Python Style

- **Type hints**: Use type hints for all function signatures
- **Async/await**: Use async/await throughout for I/O operations
- **Error handling**: Proper exception handling with context
- **Logging**: Use structured logging with appropriate levels

### Code Formatting

We use `ruff` for both linting and formatting:

```bash
# Format code
ruff format

# Check for issues
ruff check

# Fix auto-fixable issues
ruff check --fix
```

### Type Checking

Run `mypy` to ensure type safety:

```bash
mypy src/
```

## Testing

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
coverage run -m pytest
coverage report

# Run specific test file
pytest tests/test_config_loader.py

# Run with verbose output
pytest -v
```

### Writing Tests

- Use `pytest` framework
- Place tests in the `tests/` directory
- Use `pytest-asyncio` for async test functions
- Mock external dependencies appropriately

Example test structure:

```python
import pytest
from mcp_foxxy_bridge.config_loader import load_config

def test_config_loading():
    """Test basic configuration loading."""
    config = load_config("example_config.json")
    assert config is not None
    assert "mcpServers" in config
```

## Making Changes

### Development Workflow

1. **Create a feature branch**

   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes**
   - Follow code style guidelines
   - Add tests for new functionality
   - Update documentation as needed

3. **Test your changes**

   ```bash
   # Run tests
   pytest

   # Check type safety
   mypy src/

   # Check code style
   ruff check
   ruff format
   ```

4. **Commit your changes** using conventional commit format

   ```bash
   git add .
   git commit -m "feat: add your feature description"
   ```

   **Commit Message Format:**
   ```
   <type>(<scope>): <description>
   
   [optional body]
   
   [optional footer]
   ```

   **Types:**
   - `feat`: New feature
   - `fix`: Bug fix
   - `docs`: Documentation changes
   - `style`: Formatting, missing semicolons, etc.
   - `refactor`: Code refactoring
   - `test`: Adding or updating tests
   - `chore`: Build tasks, package manager configs

   **Examples:**
   ```bash
   git commit -m "feat(bridge): add environment variable expansion"
   git commit -m "fix(server): resolve connection timeout issues"
   git commit -m "docs: update installation instructions"
   ```

   To use the commit message template:
   ```bash
   git config commit.template .gitmessage
   ```

5. **Push and create a pull request**

   ```bash
   git push origin feature/your-feature-name
   ```

### Commit Message Format

Use conventional commits format:

- `feat:` - New features
- `fix:` - Bug fixes
- `docs:` - Documentation changes
- `refactor:` - Code refactoring
- `test:` - Test additions or changes
- `chore:` - Maintenance tasks

Examples:
- `feat: add environment variable expansion support`
- `fix: resolve async context manager lifecycle issue`
- `docs: update installation guide with UV commands`

### Pull Request Guidelines

- **Title**: Use a clear, descriptive title
- **Description**: Explain what changes you made and why
- **Testing**: Describe how you tested your changes
- **Breaking Changes**: Note any breaking changes
- **Documentation**: Update docs if needed

## Debugging

### Debug Mode

Run the bridge with debug logging:

```bash
uv run mcp-foxxy-bridge --bridge-config config.json --debug
```

### Common Issues

1. **Connection errors**: Check MCP server commands and arguments
2. **Port conflicts**: Bridge auto-increments ports when needed
3. **Environment variables**: Verify variable names and values
4. **Tool routing**: Check namespace configuration

### Logging

The bridge uses Python's logging module:

```python
import logging

logger = logging.getLogger(__name__)
logger.info("Bridge started successfully")
logger.error("Failed to connect to server: %s", error)
```

## Documentation

### Updating Documentation

- Update relevant documentation in `docs/` directory
- Keep `README.md` current with major changes
- Add docstrings to new functions and classes
- Update configuration examples when needed

### Documentation Style

- Use clear, concise language
- Include code examples where helpful
- Keep line lengths reasonable (80 characters preferred)
- Use proper markdown formatting

## Release Process

### Version Management

The project uses automated versioning based on Git tags:
- Versions are automatically determined from Git tags using `hatch-vcs`
- No manual version updates needed in code
- Development versions get `.devN` suffix automatically

### Creating a Release

**For Maintainers:**

1. **Prepare the release**
   - Ensure all changes are merged to `main`
   - Run tests locally to verify everything works
   
2. **Releases happen automatically via semantic-release**
   - Push conventional commits to `main` branch
   - Semantic-release analyzes commits and creates releases
   - No manual intervention needed for most releases
   
3. **Manual testing (if needed)**
   - Go to Actions → "CI/CD Pipeline" workflow  
   - Use dry-run mode to preview releases
   - Use test PyPI option for testing

**For Contributors:**
- Focus on code contributions and follow conventional commits
- Maintainers handle all releases via semantic-release
- See [docs/releasing.md](docs/releasing.md) for detailed release process

### GitHub Labels

The repository uses comprehensive labeling for organization:

- **Type labels**: `type: feature`, `type: bug`, `type: documentation`
- **Area labels**: `area: bridge`, `area: server`, `area: config`
- **Priority labels**: `priority: critical`, `priority: high`, `priority: medium`, `priority: low`
- **Status labels**: `status: in progress`, `status: needs review`, `status: blocked`
- **Size labels**: `size: XS`, `size: S`, `size: M`, `size: L`, `size: XL` (auto-applied)
- **Release labels**: `release: major`, `release: minor`, `release: patch`

Labels are automatically applied based on file changes and can be manually adjusted by maintainers.

## Getting Help

### Development Questions

- Check existing issues on GitHub
- Look at the codebase documentation
- Review test files for usage examples

### Reporting Issues

When reporting issues, include:

- Bridge version
- Configuration file (sanitized)
- Error messages and logs
- Steps to reproduce
- Environment details (OS, Python version)

### Feature Requests

For feature requests, provide:

- Clear description of the feature
- Use case and motivation
- Possible implementation approach
- Any breaking changes

## Community Guidelines

- Be respectful and constructive
- Follow the code of conduct
- Help other contributors when possible
- Focus on the project's goals and user needs

Thank you for contributing to MCP Foxxy Bridge!