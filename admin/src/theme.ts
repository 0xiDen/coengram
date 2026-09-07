import { extendTheme, type ThemeConfig } from "@chakra-ui/react";

const config: ThemeConfig = {
  initialColorMode: "light",
  useSystemColorMode: false
};

export const theme = extendTheme({
  config,
  fonts: {
    heading:
      "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    body:
      "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
  },
  colors: {
    brand: {
      50: "#eef8f6",
      100: "#cae9e4",
      200: "#9ed6cd",
      300: "#6fbeb4",
      400: "#479f99",
      500: "#2d817d",
      600: "#236966",
      700: "#205451",
      800: "#1e4442",
      900: "#1b3938"
    },
    ink: {
      50: "#f6f7f7",
      100: "#e7ebea",
      200: "#cdd5d3",
      300: "#a7b5b1",
      400: "#7d918c",
      500: "#627671",
      600: "#4d5f5b",
      700: "#3f4e4b",
      800: "#34413f",
      900: "#111817"
    },
    ember: {
      50: "#fff4ed",
      100: "#ffe1ca",
      200: "#ffc096",
      300: "#ff9862",
      400: "#fb7436",
      500: "#ec5517",
      600: "#c63d0e",
      700: "#9f2e0f",
      800: "#7f2813",
      900: "#662314"
    }
  },
  styles: {
    global: {
      body: {
        bg: "ink.50",
        color: "ink.900"
      },
      "::selection": {
        bg: "brand.200"
      }
    }
  },
  components: {
    Button: {
      defaultProps: {
        colorScheme: "brand"
      },
      baseStyle: {
        borderRadius: "8px",
        fontWeight: 650
      }
    },
    Input: {
      defaultProps: {
        focusBorderColor: "brand.500"
      }
    },
    Textarea: {
      defaultProps: {
        focusBorderColor: "brand.500"
      }
    },
    Select: {
      defaultProps: {
        focusBorderColor: "brand.500"
      }
    }
  }
});
