package dev.ratsnest.security;

import dev.ratsnest.core.DesignRun;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;

/**
 * The single ownership policy for design runs: open-mode rows (no owner)
 * are public; owned rows are visible to their owner, admins, and the
 * agent-runtime service identity. Controllers must not re-implement this.
 */
@Component
public class RunAccessPolicy {

    private static Authentication currentAuth() {
        return SecurityContextHolder.getContext().getAuthentication();
    }

    /** Logged-in username, or null for anonymous / service callers. */
    public String currentUser() {
        Authentication auth = currentAuth();
        if (auth == null || !auth.isAuthenticated()
                || "anonymousUser".equals(auth.getName())
                || "agent-runtime".equals(auth.getName())) {
            return null;
        }
        return auth.getName();
    }

    public boolean currentIsAdmin() {
        Authentication auth = currentAuth();
        return auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> a.getAuthority().contains("ADMIN")
                        || a.getAuthority().contains("SERVICE"));
    }

    public boolean canAccess(DesignRun run) {
        if (run.getOwner() == null) {
            return true;                 // open mode / legacy rows
        }
        return currentIsAdmin() || run.getOwner().equals(currentUser());
    }
}
