/*
 * SPI slave (mode 0, MSB first), oversampled by the system clock.
 * SCK must be at most clk / 8.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module pw_spi_slave (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       sck_i,
    input  wire       mosi_i,
    input  wire       csn_i,
    output wire       miso,
    output wire       active,      // CS is low
    output reg        byte_start,  // pulse: first bit of a byte arrived
    output reg        byte_valid,  // pulse: a whole byte arrived
    output reg  [7:0] byte_data,
    output reg        first_byte,  // the byte that just arrived is the command byte
    input  wire [7:0] resp,        // next byte to send, taken while byte_valid is high
    input  wire [7:0] status       // sent as the first byte of every transaction
);
  reg [2:0] sck_s, csn_s;
  reg [1:0] mosi_s;
  always @(posedge clk) begin
    if (!rst_n) begin
      sck_s <= 3'b000; mosi_s <= 2'b00; csn_s <= 3'b111;
    end else begin
      sck_s  <= {sck_s[1:0], sck_i};
      mosi_s <= {mosi_s[0], mosi_i};
      csn_s  <= {csn_s[1:0], csn_i};
    end
  end

  wire rise    = sck_s[1] & ~sck_s[2];
  wire fall    = ~sck_s[1] & sck_s[2];
  wire cs      = ~csn_s[1];
  wire cs_fall = ~csn_s[1] & csn_s[2];
  wire mosi    = mosi_s[1];

  reg [2:0] bitcnt;
  reg [6:0] sh;
  reg [7:0] tx;
  reg       got_first;

  assign miso   = tx[7];
  assign active = cs;

  always @(posedge clk) begin
    byte_valid <= 1'b0;
    byte_start <= 1'b0;
    if (!rst_n) begin
      bitcnt <= 3'd0; sh <= 7'd0; tx <= 8'd0; got_first <= 1'b0;
      first_byte <= 1'b0; byte_data <= 8'd0;
    end else if (!cs) begin
      bitcnt <= 3'd0; got_first <= 1'b0;
    end else if (cs_fall) begin
      tx <= status; bitcnt <= 3'd0; got_first <= 1'b0;
    end else begin
      if (byte_valid) tx <= resp;
      if (rise) begin
        sh     <= {sh[5:0], mosi};
        bitcnt <= bitcnt + 3'd1;
        if (bitcnt == 3'd0) byte_start <= 1'b1;
        if (bitcnt == 3'd7) begin
          byte_valid <= 1'b1;
          byte_data  <= {sh, mosi};
          first_byte <= !got_first;
          got_first  <= 1'b1;
        end
      end
      if (fall && bitcnt != 3'd0) tx <= {tx[6:0], 1'b0};
    end
  end
endmodule
