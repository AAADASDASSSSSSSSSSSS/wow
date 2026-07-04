package dev.ratsnest.core;

import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;

public interface DesignRunRepository extends JpaRepository<DesignRun, String> {
    Page<DesignRun> findByOwner(String owner, Pageable pageable);
}
