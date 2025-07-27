---
name: Manual Release Request
about: Request a manual release override (semantic-release handles most releases automatically)
title: 'Manual Release Request: [REASON]'
labels: 'release: manual, priority: high'
assignees: 'billyjbryant'
---

⚠️ **Note**: This project uses semantic-release for automatic releases based on conventional commits. Manual releases should only be requested for special circumstances.

## Release Information

**Reason for Manual Release:**
<!-- Why is a manual release needed instead of automatic semantic release? -->

**Release Type:**
- [ ] Emergency patch (critical bug fix)
- [ ] Manual version correction
- [ ] Initial release setup
- [ ] Pre-release testing
- [ ] Other: _______________

## Changes Included

<!-- List the main changes that should be included in this release -->

- [ ] Change 1 (commit: `abc123`)
- [ ] Change 2 (commit: `def456`)
- [ ] Change 3 (commit: `ghi789`)

## Semantic Release Status

- [ ] Automatic release is not working due to: _______________
- [ ] Commits don't follow conventional format and need manual release
- [ ] Emergency release needed before next push to main
- [ ] Testing pre-release functionality

## Pre-Release Checklist

- [ ] All tests are passing
- [ ] Documentation is updated
- [ ] Breaking changes are documented
- [ ] Migration guide is provided (if needed)
- [ ] Conventional commits will be used going forward

## Notes

<!-- Any additional information about this release -->

---

**For Maintainers:**
1. Verify the reason for manual release is justified
2. Consider if conventional commits can be used instead
3. Use GitHub Actions "Release" workflow with dry-run first
4. Verify automatic PyPI publishing succeeds
5. Ensure future releases use semantic-release