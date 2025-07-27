# Repository Maintenance

This document explains the automated maintenance features of the MCP Foxxy Bridge repository.

## Automatic Maintenance Triggers

The repository includes intelligent maintenance automation that responds to configuration changes and runs scheduled tasks.

### 🔄 Configuration Change Detection

When you push changes to `main` branch that modify certain configuration files, maintenance tasks run automatically:

| File Changed | Automatic Action |
|-------------|------------------|
| `.github/labels.yml` | ✅ Update all GitHub repository labels |
| `.github/labeler.yml` | ✅ Validate auto-labeler configuration |
| `.github/ISSUE_TEMPLATE/**` | ✅ Validate issue templates |  
| `.github/pull_request_template.md` | ✅ Validate PR template |
| `.releaserc.json` | ✅ Validate semantic-release configuration |
| `codecov.yml` | ✅ Validate codecov configuration |

### 📅 Scheduled Maintenance

Weekly maintenance runs every Sunday at 2 AM UTC:

- **Dependency Updates**: Creates PRs with updated `uv.lock` file
- **Security Audits**: Scans for vulnerabilities and creates issues if found
- **Artifact Cleanup**: Removes workflow artifacts older than 30 days

## Manual Maintenance

You can trigger maintenance tasks manually via GitHub Actions:

### Via GitHub UI
1. Go to **Actions** → **Repository Maintenance**
2. Click **Run workflow**
3. Select desired maintenance tasks:
   - **Set up GitHub labels**: Apply label configuration
   - **Update UV lock file**: Update dependencies  
   - **Force run all tasks**: Run all maintenance jobs

### Via CLI
```bash
# Trigger label setup
gh workflow run maintenance.yml --field setup_labels=true

# Trigger dependency update
gh workflow run maintenance.yml --field update_dependencies=true

# Run all maintenance tasks
gh workflow run maintenance.yml --field force_all=true
```

## Maintenance Jobs

### 🏷️ Label Management

**Triggers:**
- Changes to `.github/labels.yml`
- Manual trigger
- Weekly schedule

**Actions:**
- Syncs all repository labels with configuration
- Removes labels not in configuration
- Updates existing labels with new colors/descriptions
- Posts commit comment on successful updates

### 🔍 Configuration Validation

**Triggers:**
- Changes to configuration files
- Before applying configuration changes

**Validates:**
- JSON syntax in `.releaserc.json`
- YAML syntax in `codecov.yml` and `.github/labeler.yml`
- Template structure for issue/PR templates

### 📦 Dependency Updates

**Triggers:**
- Weekly schedule (Sundays 2 AM UTC)
- Manual trigger

**Process:**
1. Updates `uv.lock` with latest compatible versions
2. Runs tests with updated dependencies
3. Creates PR if changes detected
4. Auto-merges if tests pass (maintainer can disable)

**PR Details:**
- **Title**: `chore(deps): update dependencies`
- **Labels**: `type: chore`, `dependencies`, `release: skip`
- **Auto-merge**: Enabled with squash merge

### 🗂️ Artifact Cleanup

**Triggers:**
- Weekly schedule
- Manual trigger with `force_all=true`

**Actions:**
- Deletes workflow artifacts older than 30 days
- Preserves recent artifacts for debugging
- Reports cleanup statistics

### 🔒 Security Audits

**Triggers:**
- Weekly schedule
- Manual trigger with `force_all=true`

**Process:**
1. Scans dependencies for known vulnerabilities  
2. Creates high-priority security issues if vulnerabilities found
3. Includes remediation guidance and links to CVE details

**Issue Created:**
- **Title**: `🔒 Security Audit Alert - [DATE]`
- **Labels**: `security`, `priority: high`, `type: chore`
- **Assignee**: Repository maintainer

## Maintenance Feedback

### Commit Comments

Automatic maintenance posts informative comments on commits:

```markdown
🔧 Automatic Maintenance Summary

Configuration changes detected:
- 🏷️ Labels configuration
- 📊 Codecov settings

Maintenance actions completed:
- ✅ Updated GitHub labels  
- ✅ Validated configurations

Commit: abc1234
Workflow: Repository Maintenance

This maintenance was triggered automatically by configuration file changes.
```

### Status Indicators

Maintenance jobs provide clear status indicators:
- ✅ **Success**: Task completed successfully
- ❌ **Failure**: Task failed (check logs)
- ⏭️ **Skipped**: Task not needed or conditions not met

## Troubleshooting

### Label Sync Issues

If label synchronization fails:
1. Check `.github/labels.yml` syntax with YAML validator
2. Ensure proper permissions (`issues: write`, `pull-requests: write`)
3. Manually trigger: Actions → Repository Maintenance → setup_labels

### Dependency Update Failures

If dependency updates fail:
1. Check for package compatibility issues in logs
2. Review failing tests to identify breaking changes
3. Manually update problematic packages in `pyproject.toml`

### Configuration Validation Errors

If configuration validation fails:
1. Use online JSON/YAML validators
2. Check for trailing commas, quotes, indentation
3. Compare with working examples in repository

## Best Practices

### Making Configuration Changes

1. **Test locally first**: Validate JSON/YAML syntax before committing
2. **Small changes**: Make incremental changes for easier debugging
3. **Review logs**: Check maintenance workflow logs after changes
4. **Monitor results**: Verify automatic updates worked as expected

### Dependency Management

1. **Review PRs**: Check dependency update PRs before auto-merge
2. **Test locally**: Pull and test dependency updates locally when unsure
3. **Pin versions**: Pin critical dependencies to avoid breaking changes
4. **Monitor security**: Review security audit results promptly

### Label Management

1. **Consistent naming**: Use consistent label naming conventions
2. **Meaningful colors**: Choose colors that convey priority/category
3. **Clear descriptions**: Write helpful label descriptions
4. **Regular cleanup**: Remove unused labels from configuration

The maintenance system is designed to keep the repository healthy and up-to-date with minimal manual intervention while providing clear feedback about all automated actions.