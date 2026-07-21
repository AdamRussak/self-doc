package cmd

import (
	"fmt"
	"os"

	"github.com/AdamRussak/self-doc/cli/internal/format"
	"github.com/spf13/cobra"
)

var treeCmd = &cobra.Command{
	Use:   "tree",
	Short: "Print hierarchical view of indexed sources",
	Long:  `Retrieves document sources, base URLs, page counts, and chunk counts.`,
	Args:  cobra.NoArgs,
	RunE: func(cmd *cobra.Command, args []string) error {
		nodes, err := client.GetTree(cmd.Context())
		if err != nil {
			return err
		}

		out, err := format.FormatTree(nodes, jsonOutput, compact)
		if err != nil {
			return fmt.Errorf("failed to format tree output: %w", err)
		}

		fmt.Fprintln(os.Stdout, out)
		return nil
	},
}

func init() {
	RootCmd.AddCommand(treeCmd)
}
