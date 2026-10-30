package auth

import "strings"

// facetsDomains is the exhaustive list of domains whose users are Facets
// employees. Adding a domain here grants its users admin on first login, so
// this list is a security boundary — change it only deliberately.
var facetsDomains = []string{
	"facets.cloud",
	"facets.io",
}

// IsFacetsEmail reports whether email belongs to a Facets-owned domain.
//
// It matches on an exact domain, never on a substring:
// "evil@facets.attacker.com" is NOT a Facets address even though it contains
// the text "@facets.".
func IsFacetsEmail(email string) bool {
	lower := strings.ToLower(strings.TrimSpace(email))
	at := strings.LastIndex(lower, "@")
	if at < 0 || at == len(lower)-1 {
		return false
	}
	domain := lower[at+1:]
	for _, d := range facetsDomains {
		if domain == d {
			return true
		}
	}
	return false
}
