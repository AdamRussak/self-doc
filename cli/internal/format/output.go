package format

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/AdamRussak/self-doc/cli/internal/api"
)

func FormatSearch(results []api.ChunkResult, jsonOutput bool, compact bool) (string, error) {
	if jsonOutput {
		data, err := json.MarshalIndent(results, "", "  ")
		if err != nil {
			return "", err
		}
		return string(data), nil
	}

	if len(results) == 0 {
		return "No matching documentation chunks found.", nil
	}

	var sb strings.Builder
	for i, r := range results {
		if compact {
			sb.WriteString(fmt.Sprintf("[%d] %s > %s (score: %.3f)\n  %s\n  URL: %s\n", r.ID, r.Source, r.HeadingPath, r.Score, r.Snippet, r.URL))
		} else {
			if i > 0 {
				sb.WriteString("\n---\n\n")
			}
			heading := r.HeadingPath
			if heading == "" {
				heading = r.Source
			}
			sb.WriteString(fmt.Sprintf("ID: %d | Source: %s | Score: %.3f\nHeading: %s\nURL: %s\nSnippet: %s", r.ID, r.Source, r.Score, heading, r.URL, r.Snippet))
		}
	}
	return sb.String(), nil
}

func FormatChunk(detail *api.ChunkDetail, jsonOutput bool, compact bool) (string, error) {
	if jsonOutput {
		data, err := json.MarshalIndent(detail, "", "  ")
		if err != nil {
			return "", err
		}
		return string(data), nil
	}

	if compact {
		return fmt.Sprintf("# Chunk %d [%s > %s]\nURL: %s\n\n%s", detail.ID, detail.Source, detail.HeadingPath, detail.URL, strings.TrimSpace(detail.Content)), nil
	}

	var sb strings.Builder
	sb.WriteString(fmt.Sprintf("ID: %d\nSource: %s\nHeading: %s\nURL: %s\n", detail.ID, detail.Source, detail.HeadingPath, detail.URL))
	if detail.FetchedAt != nil {
		sb.WriteString(fmt.Sprintf("Fetched At: %s\n", *detail.FetchedAt))
	}
	sb.WriteString("\n---\n\n")
	sb.WriteString(detail.Content)
	return sb.String(), nil
}

func FormatTree(nodes []api.SourceNode, jsonOutput bool, compact bool) (string, error) {
	if jsonOutput {
		data, err := json.MarshalIndent(nodes, "", "  ")
		if err != nil {
			return "", err
		}
		return string(data), nil
	}

	if len(nodes) == 0 {
		return "No doc sources indexed.", nil
	}

	var sb strings.Builder
	sb.WriteString("Indexed Documentation Sources:\n")
	for _, n := range nodes {
		synced := "never"
		if n.LastSynced != nil {
			synced = *n.LastSynced
		}
		if compact {
			sb.WriteString(fmt.Sprintf("  - %s (pages: %d, chunks: %d) [%s]\n", n.Name, n.PageCount, n.ChunkCount, n.BaseURL))
		} else {
			sb.WriteString(fmt.Sprintf("  - Source: %s\n    Base URL: %s\n    Pages: %d | Chunks: %d\n    Last Synced: %s\n", n.Name, n.BaseURL, n.PageCount, n.ChunkCount, synced))
		}
	}
	return sb.String(), nil
}
