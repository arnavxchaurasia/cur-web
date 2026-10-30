package auth

import "testing"

func TestIsFacetsEmail_RejectsLookalikeDomains(t *testing.T) {
	cases := []struct {
		email string
		want  bool
	}{
		{"arnav@facets.cloud", true},
		{"ARNAV@FACETS.CLOUD", true},
		{"someone@facets.io", true},
		// Lookalikes an attacker can register — all must be false.
		{"evil@facets.attacker.com", false},
		{"evil@facets.evil.ru", false},
		{"evil@evil-facets.cloud", false},
		{"evil@facets.cloud.phish.io", false},
		{"facets.cloud@gmail.com", false},
		{"no-at-sign", false},
		{"trailing@", false},
		{"", false},
	}
	for _, c := range cases {
		if got := IsFacetsEmail(c.email); got != c.want {
			t.Errorf("IsFacetsEmail(%q) = %v, want %v", c.email, got, c.want)
		}
	}
}
