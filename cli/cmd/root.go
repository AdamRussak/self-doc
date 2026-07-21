package cmd

import (
	"fmt"
	"os"

	"github.com/AdamRussak/self-doc/cli/internal/api"
	"github.com/spf13/cobra"
)

var (
	apiURL     string
	apiToken   string
	jsonOutput bool
	compact    bool
	limit      int
	verbose    bool

	client *api.Client
)

var RootCmd = &cobra.Command{
	Use:   "doc-cli",
	Short: "Fast, progressive disclosure CLI for self-docs",
	Long:  `doc-cli is a lightweight Go CLI for querying self-docs documentation sources via the ingestion REST API.`,
	PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
		if apiURL == "" {
			apiURL = os.Getenv("SELF_DOCS_API_URL")
		}
		if apiURL == "" {
			apiURL = "http://localhost:8000"
		}

		if apiToken == "" {
			apiToken = os.Getenv("API_TOKEN")
		}
		if apiToken == "" {
			apiToken = os.Getenv("SYNC_TOKEN")
		}
		if apiToken == "" {
			apiToken = os.Getenv("MCP_TOKEN")
		}

		client = api.NewClient(apiURL, apiToken)
		return nil
	},
}

func Execute() {
	if err := RootCmd.Execute(); err != nil {
		if verbose {
			fmt.Fprintf(os.Stderr, "Error: %+v\n", err)
		} else {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
		}
		os.Exit(1)
	}
}

func init() {
	RootCmd.PersistentFlags().StringVar(&apiURL, "url", "", "Ingestion API base URL (env: SELF_DOCS_API_URL, default: http://localhost:8000)")
	RootCmd.PersistentFlags().StringVar(&apiToken, "token", "", "API auth token (env: API_TOKEN / SYNC_TOKEN / MCP_TOKEN)")
	RootCmd.PersistentFlags().BoolVar(&jsonOutput, "json", false, "Output results in JSON format")
	RootCmd.PersistentFlags().BoolVar(&compact, "compact", false, "Output results in compact single-line format")
	RootCmd.PersistentFlags().IntVar(&limit, "limit", 3, "Limit number of search results (default: 3)")
	RootCmd.PersistentFlags().BoolVar(&verbose, "verbose", false, "Enable verbose error output")
}
