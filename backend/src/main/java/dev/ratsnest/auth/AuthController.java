package dev.ratsnest.auth;

import dev.ratsnest.security.JwtService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
@RequestMapping("/api/auth")
public class AuthController {

    public record Credentials(
            @NotBlank @Size(min = 3, max = 64) String username,
            @NotBlank @Size(min = 8, max = 128) String password) {}

    private final UserAccountRepository users;
    private final PasswordEncoder passwordEncoder;
    private final JwtService jwtService;

    public AuthController(UserAccountRepository users,
                          PasswordEncoder passwordEncoder,
                          JwtService jwtService) {
        this.users = users;
        this.passwordEncoder = passwordEncoder;
        this.jwtService = jwtService;
    }

    @PostMapping("/register")
    public ResponseEntity<Map<String, String>> register(
            @Valid @RequestBody Credentials creds) {
        if (users.existsByUsername(creds.username())) {
            return ResponseEntity.status(HttpStatus.CONFLICT)
                    .body(Map.of("error", "username already taken"));
        }
        String role = users.count() == 0 ? "ADMIN" : "USER"; // first user = admin
        users.save(UserAccount.create(
                creds.username(), passwordEncoder.encode(creds.password()), role));
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(Map.of("username", creds.username(), "role", role));
    }

    @PostMapping("/login")
    public ResponseEntity<Map<String, String>> login(
            @Valid @RequestBody Credentials creds) {
        var user = users.findByUsername(creds.username()).orElse(null);
        if (user == null || !passwordEncoder.matches(
                creds.password(), user.getPasswordHash())) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED)
                    .body(Map.of("error", "invalid credentials"));
        }
        String token = jwtService.issue(user.getUsername(), user.getRole());
        return ResponseEntity.ok(Map.of(
                "token", token, "tokenType", "Bearer",
                "username", user.getUsername(), "role", user.getRole()));
    }
}
