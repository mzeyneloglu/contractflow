from pathlib import Path

FILES = {
    "src/main/java/demo/orders/OrderController.java": """
package demo.orders;

@RestController
@RequestMapping("/orders")
public class OrderController {
    private final OrderService orderService;

    @GetMapping
    public Page<OrderDto> list(@RequestParam Long customerId, Pageable pageable) {
        return orderService.activeOrders(customerId, pageable);
    }

    @GetMapping("/legacy")
    public Page<OrderDto> legacy(@RequestParam Long customerId, Pageable pageable) {
        return orderService.legacyOrders(customerId, pageable);
    }
}
""",
    "src/main/java/demo/orders/OrderService.java": """
package demo.orders;

@Service
public class OrderService {
    private final OrderRepository orderRepository;

    public Page<OrderDto> activeOrders(Long customerId, Pageable pageable) {
        return orderRepository.findActiveByCustomer(customerId, pageable).map(OrderDto::from);
    }

    public Page<OrderDto> legacyOrders(Long customerId, Pageable pageable) {
        return orderRepository.findByCustomerIdAndStatus(customerId, "A", pageable).map(OrderDto::from);
    }
}
""",
    "src/main/java/demo/orders/OrderRepository.java": """
package demo.orders;

public interface OrderRepository extends JpaRepository<OrderEntity, Long> {
    @Query(value = "SELECT o.ID FROM ORDERS o WHERE o.CUSTOMER_ID = :customerId AND o.STATUS = 'A' AND o.DELETED_AT IS NULL", nativeQuery = true)
    Page<OrderEntity> findActiveByCustomer(@Param("customerId") Long customerId, Pageable pageable);

    Page<OrderEntity> findByCustomerIdAndStatus(Long customerId, String status, Pageable pageable);
}
""",
    "src/main/java/demo/orders/OrderEntity.java": """
package demo.orders;

@Entity
@Table(name = "ORDERS")
public class OrderEntity {
    @Id
    @Column(name = "ID")
    private Long id;
    @Column(name = "CUSTOMER_ID")
    private Long customerId;
    @Column(name = "STATUS")
    private String status;
    @Column(name = "DELETED_AT")
    private LocalDateTime deletedAt;
}
""",
}


def write_demo_repository(root: Path) -> Path:
    for relative, content in FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root
