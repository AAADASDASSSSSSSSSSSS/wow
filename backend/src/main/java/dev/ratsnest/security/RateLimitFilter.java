package dev.ratsnest.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/** Per-client token bucket (in-memory, zero deps). Protects the control
 *  plane against accidental hammering; a gateway/redis limiter replaces this
 *  at true multi-node scale. Static assets and health stay unthrottled. */
@Component
public class RateLimitFilter extends OncePerRequestFilter {

    @Value("${ratsnest.ratelimit.per-second:25}")
    private double refillPerSecond;

    @Value("${ratsnest.ratelimit.burst:50}")
    private double burst;

    private static final class Bucket {
        volatile double tokens;
        final AtomicLong lastNanos = new AtomicLong(System.nanoTime());
        Bucket(double tokens) { this.tokens = tokens; }
    }

    private final Map<String, Bucket> buckets = new ConcurrentHashMap<>();

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain chain)
            throws ServletException, IOException {
        String path = request.getRequestURI();
        if (!path.startsWith("/api/") || path.equals("/api/health")) {
            chain.doFilter(request, response);
            return;
        }
        if (buckets.size() > 10_000) {
            buckets.clear(); // crude cap against key-space abuse
        }
        Bucket bucket = buckets.computeIfAbsent(clientKey(request),
                k -> new Bucket(burst));
        synchronized (bucket) {
            long now = System.nanoTime();
            double elapsed = (now - bucket.lastNanos.getAndSet(now)) / 1e9;
            bucket.tokens = Math.min(burst,
                    bucket.tokens + elapsed * refillPerSecond);
            if (bucket.tokens < 1.0) {
                response.setStatus(429);
                response.setContentType("application/json");
                response.getWriter().write(
                        "{\"type\":\"about:blank\",\"title\":\"Too Many Requests\","
                        + "\"status\":429,\"detail\":\"rate limit exceeded\"}");
                return;
            }
            bucket.tokens -= 1.0;
        }
        chain.doFilter(request, response);
    }

    private String clientKey(HttpServletRequest request) {
        String forwarded = request.getHeader("X-Forwarded-For");
        if (forwarded != null && !forwarded.isBlank()) {
            return forwarded.split(",")[0].trim();
        }
        return request.getRemoteAddr();
    }
}
