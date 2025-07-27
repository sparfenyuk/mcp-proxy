# Release Process

This document outlines the automated semantic release process for MCP Foxxy Bridge.

## Overview

The project uses **semantic-release** for fully automated versioning, changelog generation, and publishing to PyPI. Releases are triggered automatically based on conventional commit messages.

## How It Works

### 🚀 Fully Automated Releases

1. **Push commits** to `main` branch using conventional commit format
2. **Semantic-release analyzes** commits since last release
3. **Automatically determines** version bump (patch/minor/major)
4. **Generates changelog** from commit messages
5. **Creates Git tag** and GitHub release
6. **Publishes to PyPI** automatically

### 📝 Version Bumping Rules

| Commit Type | Version Bump | Example |
|-------------|--------------|---------|
| `fix:` | **Patch** (0.1.0 → 0.1.1) | Bug fixes |
| `feat:` | **Minor** (0.1.0 → 0.2.0) | New features |
| `BREAKING CHANGE:` | **Major** (0.1.0 → 1.0.0) | Breaking changes |
| `docs:`, `style:`, `test:` | **None** | No release |

### 🎯 Triggering Releases

**Automatic**: Push any qualifying commits to `main`:
```bash
git commit -m "feat: add new bridge configuration option"
git push origin main
# → Triggers minor version bump (e.g., 0.1.0 → 0.2.0)
```

**Manual**: Use GitHub Actions for testing:
```bash
# Dry run to see what would be released
Actions → CI/CD Pipeline → Run workflow → Enable "dry-run mode"

# Test release to Test PyPI
Actions → CI/CD Pipeline → Run workflow → Enable "test PyPI"
```

## No Manual Version Management

❌ **Don't manually edit version numbers**
❌ **Don't create Git tags manually** 
❌ **Don't create GitHub releases manually**

✅ **Use conventional commits**
✅ **Let semantic-release handle everything**
✅ **Focus on commit message quality**

## Version Management

### Automatic Versioning
- Version is determined by Git tags using `hatch-vcs`
- No need to manually update version in code
- Development versions get `.devN` suffix automatically

### Version File
- `src/mcp_foxxy_bridge/_version.py` is auto-generated during build
- Contains the exact version for runtime use
- Fallback to package metadata if available

## PyPI Publishing Setup

### Trusted Publishing (Recommended)
The project uses PyPI's trusted publishing feature for secure, token-free publishing:

1. **Configure in PyPI**:
   - Go to https://pypi.org/manage/project/mcp-foxxy-bridge/settings/publishing/
   - Add GitHub Actions publisher:
     - Owner: `billyjbryant`
     - Repository: `mcp-foxxy-bridge`
     - Workflow: `publish.yml`
     - Environment: `pypi`

2. **GitHub Environment Setup**:
   - Repository Settings → Environments
   - Create `pypi` environment
   - Set protection rules (require approval for production)

### Manual Token Setup (Alternative)
If trusted publishing isn't available:

1. **Generate PyPI API token** at https://pypi.org/manage/account/token/
2. **Add to GitHub Secrets**:
   - Repository Settings → Secrets → Actions
   - Add `PYPI_API_TOKEN` secret

## Testing Releases

### Test PyPI
Before publishing to production PyPI:

1. **Run CI/CD Pipeline workflow** with "Publish to Test PyPI" option enabled
2. **Install from Test PyPI**:
   ```bash
   uv pip install --index-url https://test.pypi.org/simple/ mcp-foxxy-bridge
   ```
3. **Verify functionality** before production release

### Local Testing
```bash
# Build and test locally
hatch build
uv pip install --find-links dist/ mcp-foxxy-bridge
mcp-foxxy-bridge --version
```

## Changelog Management

### Automatic Changelog Updates

The release process automatically:
1. **Updates CHANGELOG.md** with the new version entry
2. **Categorizes changes** based on conventional commit messages:
   - **Features**: `feat:` commits
   - **Bug Fixes**: `fix:` commits  
   - **Documentation**: `docs:` commits
   - **Other Changes**: All other commit types
3. **Commits the updated changelog** back to the repository
4. **Uses the changelog** for GitHub release notes

### Conventional Commits

For better changelog generation, use conventional commit format:

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Examples:**
```bash
git commit -m "feat(bridge): add environment variable expansion"
git commit -m "fix(server): resolve connection timeout issues"  
git commit -m "docs: update installation instructions"
git commit -m "chore: bump dependencies"
```

**Commit Types:**
- `feat`: New features (appears in Features section)
- `fix`: Bug fixes (appears in Bug Fixes section)
- `docs`: Documentation (appears in Documentation section)
- `style`, `refactor`, `test`, `chore`: Other changes

### Manual Changelog Updates

For major releases or complex changes, you can manually update the "Unreleased" section in CHANGELOG.md before creating a release. The automated process will move these entries to the appropriate version section.

### Changelog Format

The project follows [Keep a Changelog](https://keepachangelog.com/) format:
- **Added** for new features
- **Changed** for changes in existing functionality  
- **Deprecated** for soon-to-be removed features
- **Removed** for now removed features
- **Fixed** for any bug fixes
- **Security** for vulnerability fixes

## Post-Release

### Verification
1. **Check PyPI page**: https://pypi.org/project/mcp-foxxy-bridge/
2. **Test installation**:
   ```bash
   uv tool install mcp-foxxy-bridge
   mcp-foxxy-bridge --version
   ```
3. **Update documentation** if needed

### Announce Release
1. **Update README** with new features if needed
2. **Share on relevant channels** (GitHub Discussions, etc.)
3. **Close related issues** and PRs

## Troubleshooting

### Build Failures
- Check CI logs for specific errors
- Ensure all tests pass locally first
- Verify Python compatibility across versions

### Publishing Failures
- Check trusted publishing configuration
- Verify environment protection rules
- Ensure version doesn't already exist on PyPI

### Version Issues
- Tags must follow `v1.2.3` format exactly
- Delete and recreate tags if needed:
  ```bash
  git tag -d v1.2.3
  git push origin :refs/tags/v1.2.3
  ```

## Security Considerations

- **No API tokens in code** - Use trusted publishing
- **Environment protection** - Require approval for production
- **Signed commits** - Use GPG signing for release tags
- **Audit logs** - GitHub provides full audit trail

## Migration Notes

### From setuptools to hatch
The project uses modern Python packaging with:
- `hatchling` build backend
- `hatch-vcs` for version management
- PEP 517/518 compliant build system
- Automatic wheel and sdist generation