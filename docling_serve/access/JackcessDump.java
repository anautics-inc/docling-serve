import io.github.spannm.jackcess.Column;
import io.github.spannm.jackcess.Database;
import io.github.spannm.jackcess.DatabaseBuilder;
import io.github.spannm.jackcess.Row;
import io.github.spannm.jackcess.Table;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.util.Base64;

public final class JackcessDump {
    private static String encode(Object value) {
        String text = value == null ? "" : String.valueOf(value);
        return Base64.getEncoder().encodeToString(text.getBytes(StandardCharsets.UTF_8));
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            throw new IllegalArgumentException("usage: JackcessDump <database> <max-rows>");
        }
        int maxRows = Integer.parseInt(args[1]);
        try (Database database = DatabaseBuilder.open(new File(args[0]))) {
            for (String tableName : database.getTableNames()) {
                Table table = database.getTable(tableName);
                StringBuilder header = new StringBuilder("H\t").append(encode(tableName));
                for (Column column : table.getColumns()) {
                    header.append('\t').append(encode(column.getName()));
                }
                System.out.println(header);
                int count = 0;
                for (Row row : table) {
                    if (count++ >= maxRows) {
                        break;
                    }
                    StringBuilder values = new StringBuilder("R\t").append(encode(tableName));
                    for (Column column : table.getColumns()) {
                        values.append('\t').append(encode(row.get(column.getName())));
                    }
                    System.out.println(values);
                }
            }
        }
    }
}
