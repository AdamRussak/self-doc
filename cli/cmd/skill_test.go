package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSkillInstallCommand(t *testing.T) {
	tempDir := t.TempDir()

	targetDir = tempDir
	forceOverwrite = true
	defer func() {
		targetDir = ""
		forceOverwrite = false
	}()

	err := skillInstallCmd.RunE(skillInstallCmd, []string{})
	if err != nil {
		t.Fatalf("skillInstallCmd failed: %v", err)
	}

	expectedFile := filepath.Join(tempDir, ".agents", "skills", "doc-cli", "SKILL.md")
	content, err := os.ReadFile(expectedFile)
	if err != nil {
		t.Fatalf("failed to read installed skill file: %v", err)
	}

	if !strings.Contains(string(content), "doc-cli — Progressive Disclosure Documentation Workflow") {
		t.Errorf("unexpected skill content: %s", string(content))
	}
}

func TestResolveSkillDestPath(t *testing.T) {
	tempDir := t.TempDir()

	targetDir = tempDir
	path, err := resolveSkillDestPath()
	if err != nil {
		t.Fatalf("resolveSkillDestPath failed: %v", err)
	}

	expected := filepath.Join(tempDir, ".agents", "skills", "doc-cli", "SKILL.md")
	if path != expected {
		t.Errorf("expected %s, got %s", expected, path)
	}
}
