package format

import (
	"strings"
	"testing"

	"github.com/AdamRussak/self-doc/cli/internal/api"
)

func TestFormatSearch(t *testing.T) {
	results := []api.ChunkResult{
		{
			ID:          1,
			Source:      "fastapi",
			HeadingPath: "Path Parameters",
			URL:         "https://example.com",
			Score:       0.045,
			Snippet:     "Declare path variables",
		},
	}

	outText, err := FormatSearch(results, false, false)
	if err != nil || !strings.Contains(outText, "ID: 1") {
		t.Fatalf("unexpected text output: %s (err: %v)", outText, err)
	}

	outJSON, err := FormatSearch(results, true, false)
	if err != nil || !strings.Contains(outJSON, `"id": 1`) {
		t.Fatalf("unexpected JSON output: %s (err: %v)", outJSON, err)
	}

	outCompact, err := FormatSearch(results, false, true)
	if err != nil || !strings.Contains(outCompact, "[1] fastapi > Path Parameters") {
		t.Fatalf("unexpected compact output: %s (err: %v)", outCompact, err)
	}
}

func TestFormatChunk(t *testing.T) {
	detail := &api.ChunkDetail{
		ID:          10,
		Source:      "nextjs",
		HeadingPath: "Routing",
		URL:         "https://nextjs.org/docs",
		Content:     "# App Router Guide",
	}

	outText, err := FormatChunk(detail, false, false)
	if err != nil || !strings.Contains(outText, "# App Router Guide") {
		t.Fatalf("unexpected chunk output: %s (err: %v)", outText, err)
	}
}
