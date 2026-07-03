package dev.ratsnest;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableAsync;

@SpringBootApplication
@EnableAsync
public class RatsNestApplication {
    public static void main(String[] args) {
        SpringApplication.run(RatsNestApplication.class, args);
    }
}
