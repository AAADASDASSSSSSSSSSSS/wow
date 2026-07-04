package dev.ratsnest.security;

import com.nimbusds.jose.jwk.source.ImmutableSecret;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.NimbusJwtDecoder;
import org.springframework.security.oauth2.jwt.NimbusJwtEncoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.www.BasicAuthenticationFilter;

import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;

/**
 * Two modes, one build:
 *   open  (dev default)  — everything permitted; rate limiting still active
 *   jwt   (cluster)      — stateless bearer-token auth on all /api writes and
 *                          reads except health/auth/static; the Python worker
 *                          authenticates with X-RatsNest-Service-Token
 */
@Configuration
@EnableWebSecurity
public class SecurityConfig {

    @Value("${ratsnest.security.mode:open}")
    private String mode;

    @Value("${ratsnest.security.jwt-secret:dev-only-secret-change-me-0123456789abcdef}")
    private String jwtSecret;

    private SecretKeySpec key() {
        // HS256 needs >= 32 bytes; pad deterministically if a short dev secret sneaks in
        byte[] raw = jwtSecret.getBytes(StandardCharsets.UTF_8);
        if (raw.length < 32) {
            byte[] padded = new byte[32];
            System.arraycopy(raw, 0, padded, 0, raw.length);
            raw = padded;
        }
        return new SecretKeySpec(raw, "HmacSHA256");
    }

    @Bean
    public JwtDecoder jwtDecoder() {
        return NimbusJwtDecoder.withSecretKey(key()).build();
    }

    @Bean
    public JwtEncoder jwtEncoder() {
        return new NimbusJwtEncoder(new ImmutableSecret<>(key()));
    }

    @Bean
    public PasswordEncoder passwordEncoder() {
        return new BCryptPasswordEncoder();
    }

    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http,
                                           ServiceTokenFilter serviceTokenFilter,
                                           RateLimitFilter rateLimitFilter)
            throws Exception {
        http.csrf(csrf -> csrf.disable())
            .sessionManagement(s -> s.sessionCreationPolicy(
                    SessionCreationPolicy.STATELESS))
            .addFilterBefore(rateLimitFilter, BasicAuthenticationFilter.class)
            .addFilterBefore(serviceTokenFilter, BasicAuthenticationFilter.class);

        if ("jwt".equalsIgnoreCase(mode)) {
            http.authorizeHttpRequests(auth -> auth
                    .requestMatchers("/", "/index.html", "/assets/**",
                            "/favicon.ico", "/api/health", "/api/auth/**",
                            "/actuator/health", "/v3/api-docs/**",
                            "/swagger-ui/**", "/swagger-ui.html")
                    .permitAll()
                    .anyRequest().authenticated())
                .oauth2ResourceServer(o -> o.jwt(jwt -> {}));
        } else {
            http.authorizeHttpRequests(auth -> auth.anyRequest().permitAll());
        }
        return http.build();
    }
}
