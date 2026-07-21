package cmd

import (
	"fmt"
	"os"

	"github.com/AdamRussak/self-doc/cli/internal/format"
	"github.com/spf13/cobra"
)

var sourceFilter string

var searchCmd = &cobra.Command{
	Use:   "search <query>",
	Short: "Run progressive disclosure vector/hybrid search",
	Long:  `Executes a hybrid vector + FTS search over indexed documentation, returning candidate IDs and token-optimized snippets.`,
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		query := args[0]
		results, err := client.Search(cmd.Context(), query, sourceFilter, limit)
		if err != nil {
			return err
		}

		out, err := format.FormatSearch(results, jsonOutput, compact)
		if err != nil {
			return fmt.Errorf("failed to format search output: %w", err)
		}

		fmt.Fprintln(os.Stdout, out)
		return nil
	},
}

func init() {
	searchCmd.Flags().StringVarP(&sourceFilter, "source", "s", "", "Filter search results to a specific source name")
	RootCmd.AddCommand(searchCmd)
}
