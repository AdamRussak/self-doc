package cmd

import (
	"fmt"
	"os"
	"strconv"

	"github.com/AdamRussak/self-doc/cli/internal/format"
	"github.com/spf13/cobra"
)

var getCmd = &cobra.Command{
	Use:   "get <id>",
	Short: "Fetch exact markdown content for a chunk ID",
	Long:  `Retrieves the full text body and metadata for a specific chunk ID returned by search.`,
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		id, err := strconv.ParseInt(args[0], 10, 64)
		if err != nil {
			return fmt.Errorf("invalid chunk ID %q: must be an integer", args[0])
		}

		detail, err := client.GetChunk(cmd.Context(), id)
		if err != nil {
			return err
		}

		out, err := format.FormatChunk(detail, jsonOutput, compact)
		if err != nil {
			return fmt.Errorf("failed to format chunk output: %w", err)
		}

		fmt.Fprintln(os.Stdout, out)
		return nil
	},
}

func init() {
	RootCmd.AddCommand(getCmd)
}
