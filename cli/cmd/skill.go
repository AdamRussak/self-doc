package cmd

import (
	_ "embed"
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"
)

//go:embed skill_asset/SKILL.md
var embeddedSkillContent string

var (
	globalInstall  bool
	projectInstall bool
	targetDir      string
	forceOverwrite bool
)

var skillCmd = &cobra.Command{
	Use:   "skill",
	Short: "Manage and install the doc-cli progressive disclosure AI agent skill",
	Long:  `skill manages installation of the doc-cli SKILL.md into global or project-scoped agent customization roots.`,
}

var skillInstallCmd = &cobra.Command{
	Use:   "install",
	Short: "Install the doc-cli skill into global or project agent customization paths",
	RunE: func(cmd *cobra.Command, args []string) error {
		destPath, err := resolveSkillDestPath()
		if err != nil {
			return err
		}

		if err := os.MkdirAll(filepath.Dir(destPath), 0755); err != nil {
			return fmt.Errorf("failed to create directory %s: %w", filepath.Dir(destPath), err)
		}

		if _, err := os.Stat(destPath); err == nil && !forceOverwrite {
			fmt.Printf("Skill already exists at %s. Use --force to overwrite.\n", destPath)
			return nil
		}

		if err := os.WriteFile(destPath, []byte(embeddedSkillContent), 0644); err != nil {
			return fmt.Errorf("failed to write skill file %s: %w", destPath, err)
		}

		fmt.Printf("✓ Successfully installed doc-cli skill to:\n  %s\n", destPath)
		return nil
	},
}

var skillStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Check installation status and environment health for doc-cli skill",
	RunE: func(cmd *cobra.Command, args []string) error {
		homeDir, err := os.UserHomeDir()
		if err != nil {
			homeDir = "."
		}

		globalPath := filepath.Join(homeDir, ".gemini", "config", "skills", "doc-cli", "SKILL.md")
		projectPath := filepath.Join(".", ".agents", "skills", "doc-cli", "SKILL.md")

		fmt.Println("doc-cli Skill & Diagnostic Status:")

		if _, err := os.Stat(globalPath); err == nil {
			fmt.Printf("  Global Skill:   ✓ Installed (%s)\n", globalPath)
		} else {
			fmt.Printf("  Global Skill:   - Not installed (%s)\n", globalPath)
		}

		if _, err := os.Stat(projectPath); err == nil {
			fmt.Printf("  Project Skill:  ✓ Installed (%s)\n", projectPath)
		} else {
			fmt.Printf("  Project Skill:  - Not installed (%s)\n", projectPath)
		}

		fmt.Printf("  API Base URL:   %s\n", apiURL)
		if apiToken != "" {
			fmt.Println("  Auth Token:     ✓ Configured")
		} else {
			fmt.Println("  Auth Token:     - Missing (set API_TOKEN / SYNC_TOKEN / MCP_TOKEN)")
		}

		err = client.Ping()
		if err != nil {
			fmt.Printf("  API Reachable:  - Unreachable (%s)\n", err)
		} else {
			fmt.Println("  API Reachable:  ✓ Connected")
		}

		return nil
	},
}

func resolveSkillDestPath() (string, error) {
	if targetDir != "" {
		return filepath.Join(targetDir, ".agents", "skills", "doc-cli", "SKILL.md"), nil
	}

	if projectInstall {
		return filepath.Join(".", ".agents", "skills", "doc-cli", "SKILL.md"), nil
	}

	homeDir, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("could not determine user home directory: %w", err)
	}

	return filepath.Join(homeDir, ".gemini", "config", "skills", "doc-cli", "SKILL.md"), nil
}

func init() {
	skillInstallCmd.Flags().BoolVarP(&globalInstall, "global", "g", true, "Install skill to global agent customization root (~/.gemini/config/skills/doc-cli/SKILL.md)")
	skillInstallCmd.Flags().BoolVarP(&projectInstall, "project", "p", false, "Install skill to current project root (.agents/skills/doc-cli/SKILL.md)")
	skillInstallCmd.Flags().StringVarP(&targetDir, "dir", "d", "", "Target project directory for skill installation (<dir>/.agents/skills/doc-cli/SKILL.md)")
	skillInstallCmd.Flags().BoolVarP(&forceOverwrite, "force", "f", false, "Overwrite existing SKILL.md file if present")

	skillCmd.AddCommand(skillInstallCmd)
	skillCmd.AddCommand(skillStatusCmd)

	RootCmd.AddCommand(skillCmd)
}
